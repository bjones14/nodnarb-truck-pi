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
AWAKE_THRESHOLD_V = 13.0  # Threshold to consider engine "Running"
IGNITION_OFF_DELAY_S = 120 # 2 minutes debounce for stop-start
TELEMETRY_WINDOW_S = 300   # 5 minutes for hourly wakeups
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
    """Manages ignition-based shutdown and telemetry window logic."""
    def __init__(self):
        self.boot_time = time.monotonic()
        self.low_ign_start_time = None
        self.is_telemetry_mode = True # Default to True, checked at first ADC read
        self.shutdown_triggered = False

    def update(self, current_ign_v):
        now = time.monotonic()
        is_engine_running = current_ign_v > AWAKE_THRESHOLD_V

        # On the very first valid read, determine if we are in driving or telemetry mode
        if now - self.boot_time < 5 and is_engine_running:
            self.is_telemetry_mode = False
            logging.info("PowerManager: Detected Engine Running at boot. Entering Driving Mode.")
        
        # If the engine is running, we are definitely NOT in telemetry mode anymore
        if is_engine_running:
            self.is_telemetry_mode = False
            self.low_ign_start_time = None
            return False

        # --- LOGIC FOR ENGINE OFF ---
        if self.is_telemetry_mode:
            # We woke up for telemetry. Stay on for the defined window.
            if (now - self.boot_time) > TELEMETRY_WINDOW_S:
                logging.info(f"PowerManager: Telemetry window ({TELEMETRY_WINDOW_S}s) expired. Shutting down.")
                return True
        else:
            # We were driving, but engine is now off. Start the debounce timer.
            if self.low_ign_start_time is None:
                self.low_ign_start_time = now
                logging.info("PowerManager: Ignition lost. Starting shutdown debounce timer.")
            
            elapsed_off = now - self.low_ign_start_time
            if elapsed_off > IGNITION_OFF_DELAY_S:
                logging.info(f"PowerManager: Ignition low for {int(elapsed_off)}s. Shutting down.")
                return True
        
        return False

    def trigger_shutdown(self):
        if not self.shutdown_triggered:
            self.shutdown_triggered = True
            logging.warning("SYSTEM SHUTDOWN INITIATED BY POWER MANAGER.")
            os.system("sudo shutdown -h now")

class TruckFanController:
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
    def __init__(self, capacity_ah):
        self.capacity_ah = capacity_ah
        self.soc = 1.0
        self.load_state()

    def update(self, amps, volts, dt):
        self.soc += (amps * dt / 3600.0 / self.capacity_ah)
        self.soc = max(0.0, min(1.0, self.soc))
        if volts >= 13.2 and amps >= -0.5:
            self.soc = 1.0
        return self.soc * 100.0

    def save_state(self):
        try:
            with open(SOC_STATE_FILE, "w") as f:
                json.dump({"soc_fraction": self.soc, "timestamp": time.time()}, f)
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
            logging.info("ADS1115 Hardware initialized successfully.")
        except Exception as e:
            logging.error(f"ADS1115 Init Error: {e}")

def bcd2dec(val):
    return (val // 16 * 10) + (val & 0x0F)

def sync_time_from_witty():
    try:
        import smbus
        bus = smbus.SMBus(1)
        ADDR = 0x08
        sec = bcd2dec(bus.read_byte_data(ADDR, 58) & 0x7F)
        minute = bcd2dec(bus.read_byte_data(ADDR, 59))
        hour = bcd2dec(bus.read_byte_data(ADDR, 60))
        day = bcd2dec(bus.read_byte_data(ADDR, 61))
        month = bcd2dec(bus.read_byte_data(ADDR, 63))
        year = bcd2dec(bus.read_byte_data(ADDR, 64)) + 2000
        bus.close()
        time_str = f"{year}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{sec:02d}"
        os.system(f"sudo date -s '{time_str}' > /dev/null 2>&1")
        logging.info(f"System time forced to Witty Pi RTC: {time_str}")
    except Exception as e:
        logging.error(f"Failed to sync time from RTC: {e}")

def get_witty_data():
    data = {"vin": 0.0, "vout": 0.0, "iout": 0.0, "temp_c": 0.0, "rtc_time": "Unknown"}
    try:
        import smbus
        bus = smbus.SMBus(1)
        ADDR = 0x08
        vin_i = bus.read_byte_data(ADDR, 1)
        vin_d = bus.read_byte_data(ADDR, 2)
        data["vin"] = round(vin_i + (vin_d / 100.0), 2)
        vout_i = bus.read_byte_data(ADDR, 3)
        vout_d = bus.read_byte_data(ADDR, 4)
        data["vout"] = round(vout_i + (vout_d / 100.0), 2)
        iout_i = bus.read_byte_data(ADDR, 5)
        iout_d = bus.read_byte_data(ADDR, 6)
        data["iout"] = round(iout_i + (iout_d / 100.0), 2)
        temp_bytes = bus.read_i2c_block_data(ADDR, 50, 2)
        t_data = (temp_bytes[0] << 8) | temp_bytes[1]
        t_data = t_data >> 5
        if t_data >= 0x400:
            t_data = (t_data & 0x3FF) - 1024
        data["temp_c"] = round(t_data * 0.125, 1)
        sec = bcd2dec(bus.read_byte_data(ADDR, 58) & 0x7F)
        minute = bcd2dec(bus.read_byte_data(ADDR, 59))
        hour = bcd2dec(bus.read_byte_data(ADDR, 60))
        day = bcd2dec(bus.read_byte_data(ADDR, 61))
        month = bcd2dec(bus.read_byte_data(ADDR, 63))
        year = bcd2dec(bus.read_byte_data(ADDR, 64)) + 2000
        data["rtc_time"] = f"{year}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{sec:02d}"
        bus.close()
    except Exception as e:
        logging.error(f"Native SMBus Error: {e}")
    return data

def publish_ha_discovery(client):
    device = {"identifiers": ["truck_telemetry_v8"], "name": "Silverado Telemetry Node"}
    sensors = [
        {"id": "batt_v", "name": "Battery (ADC)", "cls": "voltage", "unit": "V", "tpl": "{{ value_json.battery_voltage }}"},
        {"id": "ign_v", "name": "Ignition Signal", "cls": "voltage", "unit": "V", "tpl": "{{ value_json.ign_voltage }}"},
        {"id": "w_vin", "name": "Battery (Witty)", "cls": "voltage", "unit": "V", "tpl": "{{ value_json.witty_vin }}"},
        {"id": "curr", "name": "Battery Current", "cls": "current", "unit": "A", "tpl": "{{ value_json.current_amps }}"},
        {"id": "soc", "name": "Battery SoC", "cls": "battery", "unit": "%", "tpl": "{{ value_json.soc_percent | round(1) }}"},
        {"id": "cpu_t", "name": "Pi CPU Temp", "unit": "°C", "tpl": "{{ value_json.cpu_temp_c }}", "icon": "mdi:thermometer"},
        {"id": "fan_rpm", "name": "Fan Speed (RPM)", "unit": "RPM", "tpl": "{{ value_json.fan_rpm }}", "icon": "mdi:fan-speed"},
        {"id": "awake", "name": "Truck Power Status", "cmp": "binary_sensor", "cls": "power", "tpl": "{{ 'ON' if value_json.truck_awake else 'OFF' }}"}
    ]
    for s in sensors:
        cmp = s.get("cmp", "sensor")
        topic = f"homeassistant/{cmp}/truck_v8/{s['id']}/config"
        payload = {
            "name": s['name'],
            "state_topic": f"{BASE_TOPIC}/system",
            "value_template": s['tpl'],
            "unique_id": f"truck_v8_{s['id']}",
            "device": device,
            "unit_of_measurement": s.get("unit")
        }
        if "cls" in s: payload["device_class"] = s["cls"]
        if "icon" in s: payload["icon"] = s["icon"]
        client.publish(topic, json.dumps(payload), retain=True)

def main():
    init_hardware()
    sync_time_from_witty()

    fan = TruckFanController(FAN_TACH_GPIO)
    tracker = BatteryTracker(BATTERY_CAPACITY_AH)
    pwr_manager = PowerManager()

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except (AttributeError, TypeError):
        client = mqtt.Client()

    client.username_pw_set(MQTT_USER, MQTT_PASS)

    try:
        client.connect(BROKER_IP, BROKER_PORT, 60)
        client.loop_start()
        publish_ha_discovery(client)
    except Exception as e:
        logging.error(f"MQTT Connect Error: {e}")

    last_time = time.monotonic()

    while True:
        try:
            now = time.monotonic()
            witty_cache = get_witty_data()

            # Read CPU Temp
            cpu_c = 0.0
            try:
                with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                    cpu_c = round(int(f.read()) / 1000.0, 1)
            except Exception: pass

            # Fan logic
            fan_speed_target = 0
            if cpu_c > 55: fan_speed_target = 40
            if cpu_c > 65: fan_speed_target = 75
            if cpu_c > 75: fan_speed_target = 100
            current_fan_speed = fan.set_speed(fan_speed_target)
            current_fan_rpm = fan.get_rpm()

            # ADC Readings
            main_v = 0.0
            ign_v = 0.0
            amps = 0.0
            try:
                if chan_main_v:
                    main_v = round(chan_main_v.voltage * DIVIDER_FACTOR, 2)
                if chan_ign_v:
                    ign_v = round(chan_ign_v.voltage * DIVIDER_FACTOR, 2)
                if chan_curr_vout and chan_curr_vref:
                    raw_diff = chan_curr_vout.voltage - chan_curr_vref.voltage
                    amps = round((raw_diff - CURRENT_ZERO_OFFSET_V) / CURRENT_SENSITIVITY_V_PER_A, 2)
            except Exception: pass

            # --- POWER MANAGEMENT LOGIC ---
            if pwr_manager.update(ign_v):
                pwr_manager.trigger_shutdown()

            is_awake = ign_v > AWAKE_THRESHOLD_V
            dt = now - last_time
            last_time = now
            soc = tracker.update(amps, main_v, dt)

            payload = {
                "battery_voltage": main_v,
                "ign_voltage": ign_v,
                "witty_vin": witty_cache.get("vin", 0.0),
                "witty_iout": witty_cache.get("iout", 0.0),
                "current_amps": amps,
                "soc_percent": soc,
                "cpu_temp_c": cpu_c,
                "fan_rpm": current_fan_rpm,
                "truck_awake": is_awake,
                "rtc_time": witty_cache.get("rtc_time", "Unknown"),
                "sys_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }

            client.publish(f"{BASE_TOPIC}/system", json.dumps(payload))

            try:
                with open("/dev/shm/telemetry.json", "w") as f:
                    json.dump(payload, f)
            except Exception: pass

            if int(now) % 300 < 2:
                tracker.save_state()

            time.sleep(1)

        except Exception as e:
            logging.critical(f"CRASH IN MAIN LOOP: {e}")
            logging.critical(traceback.format_exc())
            time.sleep(5)

if __name__ == "__main__":
    main()

