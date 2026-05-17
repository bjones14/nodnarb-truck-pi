import datetime
import json
import logging
import os
import sys
import time
import traceback

# --- DIAGNOSTIC LOGGING ---
LOG_FILE = '/home/brandon/telemetry_debug.log'
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

sys.dont_write_bytecode = True

# --- LIBRARIES ---
try:
    import psutil
except ImportError:
    psutil = None

try:
    import paho.mqtt.client as mqtt
except ImportError:
    logging.critical("CRITICAL: 'paho-mqtt' not found.")
    sys.exit(1)

try:
    import board
    import busio
    import adafruit_ads1x15.ads1115 as ADS
    from adafruit_ads1x15.analog_in import AnalogIn
except ImportError as e:
    logging.error(f"Adafruit libraries import failed: {e}")
    board = busio = ADS = AnalogIn = None

try:
    from gpiozero import DigitalInputDevice
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False

# --- CONFIG ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'config.json')

try:
    with open(CONFIG_PATH, 'r') as f:
        config = json.load(f)
except Exception:
    config = {
        "mqtt": {
            "broker": "homeassistant",
            "port": 1883,
            "user": "truck",
            "pass": "truck"
        }
    }

BROKER_IP = config.get('mqtt', {}).get('broker', 'homeassistant')
BROKER_PORT = config.get('mqtt', {}).get('port', 1883)
MQTT_USER = config.get('mqtt', {}).get('user', 'truck')
MQTT_PASS = config.get('mqtt', {}).get('pass', 'truck')
BASE_TOPIC = "truck/pi"

# --- CALIBRATION ---
DIVIDER_FACTOR = 5.545
VOLTAGE_ADC_DIVIDER_FACTOR = 2.0
AWAKE_THRESHOLD_V = 12.5
CHARGER_ENTER_V = 12.90
CHARGER_EXIT_V = 12.75
IGNITION_OFF_V = 2.0
IGNITION_OFF_DELAY_S = 120
TELEMETRY_WINDOW_S = 300
CURRENT_SENSITIVITY_V_PER_A = 0.003125
CURRENT_ZERO_OFFSET_V = 0.0074
BATTERY_CAPACITY_AH = 80.0
SOC_STATE_FILE = os.path.join(SCRIPT_DIR, "soc_state.json")

# --- FAN & PWM ---
PWM_CHIP_PATH = "/sys/class/pwm/pwmchip0"
PWM_FULL_PATH = f"{PWM_CHIP_PATH}/pwm0"
PWM_PERIOD_NS = 40000
FAN_TACH_GPIO = 15

# --- GLOBAL STATE ---
chan_main_v = None
chan_ign_v = None
chan_curr_vout = None
chan_curr_vref = None


class PowerManager:
    """Manages power states with hysteresis for garage charging."""

    def __init__(self):
        self.boot_time = time.monotonic()
        self.low_ign_start_time = None
        self.is_telemetry_mode = True
        self.is_charging_mode = False
        self.shutdown_triggered = False

    def update(self, current_ign_v, current_main_v):
        """Returns (should_shutdown, state_string)"""
        now = time.monotonic()

        # 1. DRIVING
        if current_ign_v > AWAKE_THRESHOLD_V:
            self.is_telemetry_mode = False
            self.is_charging_mode = False
            self.low_ign_start_time = None
            return False, "DRIVING"

        # 2. CHARGING (Garage Mode)
        if current_ign_v < IGNITION_OFF_V:
            if not self.is_charging_mode:
                if current_main_v >= CHARGER_ENTER_V:
                    self.is_charging_mode = True
                    logging.info(f"PowerManager: Charger detected ({current_main_v}V).")
            else:
                if current_main_v < CHARGER_EXIT_V:
                    self.is_charging_mode = False
                    logging.info(f"PowerManager: Charger lost ({current_main_v}V).")

        if self.is_charging_mode:
            self.low_ign_start_time = None
            return False, "CHARGING"

        # 3. TELEMETRY
        if self.is_telemetry_mode:
            if (now - self.boot_time) > TELEMETRY_WINDOW_S:
                return True, "SLEEPING"
            return False, "TELEMETRY"

        # 4. SHUTDOWN_DEBOUNCE
        if self.low_ign_start_time is None:
            self.low_ign_start_time = now

        elapsed_off = now - self.low_ign_start_time
        if elapsed_off > IGNITION_OFF_DELAY_S:
            return True, "SHUTDOWN_PENDING"

        return False, "SHUTDOWN_DEBOUNCE"

    def trigger_shutdown(self):
        if not self.shutdown_triggered:
            self.shutdown_triggered = True
            logging.warning("SYSTEM SHUTDOWN INITIATED.")
            os.system("sudo shutdown -h now")


class TruckFanController:
    """Controls Pi cooling based on thermal thresholds."""

    def __init__(self, tach_pin):
        self.tach_pulses = 0
        self.last_tach_time = time.monotonic()
        self.current_rpm = 0

        try:
            if not os.path.exists(PWM_FULL_PATH):
                with open(f"{PWM_CHIP_PATH}/export", "w") as f:
                    f.write("0")
                time.sleep(0.5)
                with open(f"{PWM_FULL_PATH}/period", "w") as f:
                    f.write(str(PWM_PERIOD_NS))
                with open(f"{PWM_FULL_PATH}/enable", "w") as f:
                    f.write("1")
        except Exception:
            pass

        if GPIO_AVAILABLE:
            try:
                self.tach = DigitalInputDevice(tach_pin, pull_up=True)
                self.tach.when_activated = self._count_pulse
            except Exception:
                pass

    def _count_pulse(self):
        self.tach_pulses += 1

    def set_speed(self, percent):
        percent = max(0, min(100, percent))
        duty_cycle = int((percent / 100.0) * PWM_PERIOD_NS)
        try:
            with open(f"{PWM_FULL_PATH}/duty_cycle", "w") as f:
                f.write(str(duty_cycle))
        except Exception:
            pass
        return percent

    def get_rpm(self):
        now = time.monotonic()
        dt = now - self.last_tach_time
        if dt > 1.0:
            self.current_rpm = (self.tach_pulses * 30.0) / dt
            self.tach_pulses = 0
            self.last_tach_time = now
        return int(self.current_rpm)


class BatteryTracker:
    """Hybrid SoC Tracker: OCV Anchor at boot + Integrated Coulomb Counting."""

    def __init__(self, capacity_ah):
        self.capacity_ah = capacity_ah
        self.soc = 1.0
        self.load_state()

    def get_soc_from_voltage(self, volts):
        """Estimate SoC based on resting AGM voltage (Open Circuit)."""
        if volts >= 12.85:
            return 1.0
        if volts >= 12.65:
            return 0.85
        if volts >= 12.50:
            return 0.75
        if volts >= 12.35:
            return 0.60
        if volts >= 12.20:
            return 0.50
        if volts >= 12.10:
            return 0.40
        if volts >= 12.00:
            return 0.30
        if volts >= 11.80:
            return 0.15
        return 0.0

    def update(self, amps, volts, dt, temp_c=25.0):
        temp_factor = 1.0
        if temp_c < 25:
            temp_factor = 1.0 - (0.01 * (25 - temp_c))
            temp_factor = max(0.6, temp_factor)

        eff_capacity = self.capacity_ah * temp_factor
        efficiency = 0.95 if amps > 0 else 1.0
        delta_ah = (amps * (dt / 3600.0)) * efficiency
        self.soc += (delta_ah / eff_capacity)
        self.soc = max(0.0, min(1.0, self.soc))

        if volts >= 13.4 and amps > -0.5:
            self.soc = 1.0

        return self.soc * 100.0

    def save_state(self):
        try:
            with open(SOC_STATE_FILE, "w") as f:
                json.dump({"soc_fraction": self.soc, "time": time.time()}, f)
        except Exception:
            pass

    def load_state(self):
        try:
            if os.path.exists(SOC_STATE_FILE):
                with open(SOC_STATE_FILE, "r") as f:
                    self.soc = json.load(f).get("soc_fraction", 1.0)
        except Exception:
            pass


def init_hardware():
    global chan_main_v, chan_ign_v, chan_curr_vout, chan_curr_vref
    if board and busio and ADS:
        try:
            i2c_bus = busio.I2C(board.SCL, board.SDA)
            ads = ADS.ADS1115(i2c_bus)
            ads.gain = 1
            chan_main_v = AnalogIn(ads, 0)
            chan_ign_v = AnalogIn(ads, 1)
            chan_curr_vout = AnalogIn(ads, 2)
            chan_curr_vref = AnalogIn(ads, 3)
            logging.info("ADS1115 hardware initialized.")
        except Exception as e:
            logging.error(f"ADS1115 Init Error: {e}")


def bcd2dec(val):
    return (val // 16 * 10) + (val & 0x0F)


def sync_time_from_witty():
    try:
        import smbus
        bus = smbus.SMBus(1)
        addr = 0x08
        sec = bcd2dec(bus.read_byte_data(addr, 58) & 0x7F)
        minute = bcd2dec(bus.read_byte_data(addr, 59))
        hour = bcd2dec(bus.read_byte_data(addr, 60))
        day = bcd2dec(bus.read_byte_data(addr, 61))
        month = bcd2dec(bus.read_byte_data(addr, 63))
        year = bcd2dec(bus.read_byte_data(addr, 64)) + 2000
        bus.close()
        time_str = f"{year}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{sec:02d}"
        os.system(f"sudo date -s '{time_str}' > /dev/null 2>&1")
    except Exception:
        pass


def get_witty_data():
    data = {
        "vin": 0.0, "vout": 0.0, "iout": 0.0,
        "temp_c": 0.0, "rtc_time": "Unknown"
    }
    try:
        import smbus
        bus = smbus.SMBus(1)
        addr = 0x08
        vin_i = bus.read_byte_data(addr, 1)
        vin_d = bus.read_byte_data(addr, 2)
        data["vin"] = round(vin_i + (vin_d / 100.0), 2)

        vout_i = bus.read_byte_data(addr, 3)
        vout_d = bus.read_byte_data(addr, 4)
        data["vout"] = round(vout_i + (vout_d / 100.0), 2)

        iout_i = bus.read_byte_data(addr, 5)
        iout_d = bus.read_byte_data(addr, 6)
        data["iout"] = round(iout_i + (iout_d / 100.0), 2)

        temp_bytes = bus.read_i2c_block_data(addr, 50, 2)
        t_raw = ((temp_bytes[0] << 8) | temp_bytes[1]) >> 5
        if t_raw >= 0x400:
            t_raw -= 1024
        data["temp_c"] = round(t_raw * 0.125, 1)

        sec = bcd2dec(bus.read_byte_data(addr, 58) & 0x7F)
        minute = bcd2dec(bus.read_byte_data(addr, 59))
        hour = bcd2dec(bus.read_byte_data(addr, 60))
        day = bcd2dec(bus.read_byte_data(addr, 61))
        month = bcd2dec(bus.read_byte_data(addr, 63))
        year = bcd2dec(bus.read_byte_data(addr, 64)) + 2000
        data["rtc_time"] = f"{year}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{sec:02d}"
        bus.close()
    except Exception:
        pass
    return data


def publish_ha_discovery(client):
    device = {
        "identifiers": ["truck_telemetry_v8"],
        "name": "Silverado Telemetry Node"
    }
    sensors = [
        {"id": "batt_v", "name": "Battery (ADC)", "cls": "voltage", "unit": "V", "tpl": "{{ value_json.battery_voltage }}"},
        {"id": "w_vin", "name": "Battery (WittyPi)", "cls": "voltage", "unit": "V", "tpl": "{{ value_json.witty_vin }}"},
        {"id": "ign_v", "name": "Ignition (ADC)", "cls": "voltage", "unit": "V", "tpl": "{{ value_json.ign_voltage }}"},
        {"id": "v_adc", "name": "Voltage (ADC)", "cls": "voltage", "unit": "V", "tpl": "{{ value_json.voltage_adc }}"},
        {"id": "curr", "name": "Current (ADC)", "cls": "current", "unit": "A", "tpl": "{{ value_json.current_amps }}"},
        {"id": "soc", "name": "Battery SoC", "cls": "battery", "unit": "%", "tpl": "{{ value_json.soc_percent | round(1) }}"},
        {"id": "pwr_state", "name": "Power State", "tpl": "{{ value_json.power_state }}", "icon": "mdi:state-machine"},
        {"id": "cpu_t", "name": "Pi CPU Temp", "unit": "°C", "tpl": "{{ value_json.cpu_temp_c }}", "icon": "mdi:thermometer"},
        {"id": "cpu_u", "name": "Pi CPU Usage", "unit": "%", "tpl": "{{ value_json.cpu_usage }}", "icon": "mdi:cpu-64-bit"},
        {"id": "w_iout", "name": "Current (WittyPi)", "cls": "current", "unit": "A", "tpl": "{{ value_json.witty_iout }}"},
        {"id": "w_vout", "name": "Voltage (WittyPi)", "cls": "voltage", "unit": "V", "tpl": "{{ value_json.witty_vout }}"},
        {"id": "w_temp", "name": "WittyPi Temp", "unit": "°C", "tpl": "{{ value_json.witty_temp_c }}", "icon": "mdi:thermometer"},
        {"id": "fan_rpm", "name": "Fan Speed (RPM)", "unit": "RPM", "tpl": "{{ value_json.fan_rpm }}", "icon": "mdi:fan-speed"},
        {"id": "rtc_t", "name": "RTC Time", "tpl": "{{ value_json.rtc_time }}", "icon": "mdi:clock-outline"},
        {"id": "sys_t", "name": "System Time", "tpl": "{{ value_json.sys_time }}", "icon": "mdi:clock-digital"}
    ]
    for s in sensors:
        cmp = s.get("cmp", "sensor")
        topic = f"homeassistant/{cmp}/truck_v8/{s['id']}/config"
        payload = {
            "name": s['name'], "state_topic": f"{BASE_TOPIC}/system",
            "value_template": s['tpl'], "unique_id": f"truck_v8_{s['id']}",
            "device": device
        }
        if s.get("unit"): payload["unit_of_measurement"] = s["unit"]
        if s.get("cls"): payload["device_class"] = s["cls"]
        if s.get("icon"): payload["icon"] = s["icon"]
        client.publish(topic, json.dumps(payload), retain=True)


def main():
    init_hardware()
    sync_time_from_witty()

    fan = TruckFanController(FAN_TACH_GPIO)
    tracker = BatteryTracker(BATTERY_CAPACITY_AH)
    pwr_manager = PowerManager()

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except Exception:
        client = mqtt.Client()

    client.username_pw_set(MQTT_USER, MQTT_PASS)

    try:
        client.connect(BROKER_IP, BROKER_PORT, 60)
        client.loop_start()
        publish_ha_discovery(client)
    except Exception as e:
        logging.error(f"MQTT Connect Error: {e}")

    last_time = time.monotonic()
    is_first_loop = True

    while True:
        try:
            now = time.monotonic()
            witty = get_witty_data()

            cpu_usage = 0.0
            if psutil:
                cpu_usage = psutil.cpu_percent()

            cpu_c = 0.0
            try:
                with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                    cpu_c = round(int(f.read()) / 1000.0, 1)
            except Exception:
                pass

            fan_speed = 0
            if cpu_c > 55: fan_speed = 40
            if cpu_c > 65: fan_speed = 75
            if cpu_c > 75: fan_speed = 100
            fan.set_speed(fan_speed)
            fan_rpm = fan.get_rpm()

            main_v = 0.0; ign_v = 0.0; amps = 0.0; v_adc = 0.0
            if chan_main_v:
                main_v = round(chan_main_v.voltage * DIVIDER_FACTOR, 3)
            if chan_ign_v:
                ign_v = round(chan_ign_v.voltage * DIVIDER_FACTOR, 3)
            if chan_curr_vout and chan_curr_vref:
                # Use factor of 2.0 to show the 5V supply rail health on the dashboard
                v_adc = round(chan_curr_vref.voltage * VOLTAGE_ADC_DIVIDER_FACTOR, 3)
                diff = chan_curr_vout.voltage - chan_curr_vref.voltage
                amps = round((diff - CURRENT_ZERO_OFFSET_V) / CURRENT_SENSITIVITY_V_PER_A, 2)

            if is_first_loop and main_v > 1.0:
                tracker.soc = tracker.get_soc_from_voltage(main_v)
                logging.info(f"SoC Initialized via OCV: {tracker.soc * 100.0}%")
                is_first_loop = False

            should_shutdown, pwr_state = pwr_manager.update(ign_v, main_v)
            if should_shutdown:
                pwr_manager.trigger_shutdown()

            dt = now - last_time
            last_time = now
            soc = tracker.update(amps, main_v, dt, temp_c=witty["temp_c"])

            payload = {
                "battery_voltage": round(main_v, 2),
                "ign_voltage": round(ign_v, 2),
                "voltage_adc": round(v_adc, 2),
                "power_state": pwr_state,
                "truck_awake": (pwr_state in ["DRIVING", "CHARGING"]),
                "witty_vin": witty["vin"],
                "witty_vout": witty["vout"],
                "witty_iout": witty["iout"],
                "witty_temp_c": witty["temp_c"],
                "current_amps": amps,
                "soc_percent": soc,
                "cpu_temp_c": cpu_c,
                "cpu_usage": cpu_usage,
                "fan_rpm": fan_rpm,
                "rtc_time": witty["rtc_time"],
                "sys_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }

            client.publish(f"{BASE_TOPIC}/system", json.dumps(payload))
            if int(now) % 300 < 2:
                tracker.save_state()
            time.sleep(1)

        except Exception as e:
            logging.critical(f"CRASH: {e}\n{traceback.format_exc()}")
            time.sleep(5)


if __name__ == "__main__":
    main()

