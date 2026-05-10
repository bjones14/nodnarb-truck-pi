import json
import os
import sys
import time
import logging
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
    config = {"mqtt": {"broker": "homeassistant", "port": 1883, "user": "truck", "pass": "truck"}}

BROKER_IP = config.get('mqtt', {}).get('broker', 'homeassistant')
BROKER_PORT = config.get('mqtt', {}).get('port', 1883)
MQTT_USER = config.get('mqtt', {}).get('user', 'truck')
MQTT_PASS = config.get('mqtt', {}).get('pass', 'truck')
BASE_TOPIC = "truck/pi"

# --- CALIBRATION ---
DIVIDER_FACTOR = 5.545
AWAKE_THRESHOLD_V = 9.0
CURRENT_SENSITIVITY_V_PER_A = 0.003125
CURRENT_ZERO_OFFSET_V = 0.0074
AMP_CRANK_THRESHOLD = 40.0
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

class TruckFanController:
    def __init__(self, tach_pin):
        self.tach_pulses = 0
        self.last_tach_time = time.monotonic()
        self.current_rpm = 0
        try:
            if not os.path.exists(PWM_FULL_PATH):
                with open(f"{PWM_CHIP_PATH}/export", "w") as f: f.write("0")
                time.sleep(0.5)
                with open(f"{PWM_FULL_PATH}/period", "w") as f: f.write(str(PWM_PERIOD_NS))
                with open(f"{PWM_FULL_PATH}/enable", "w") as f: f.write("1")
        except Exception: pass
            
        if GPIO_AVAILABLE:
            try:
                self.tach = DigitalInputDevice(tach_pin, pull_up=True)
                self.tach.when_activated = self._count_pulse
            except Exception: pass

    def _count_pulse(self): self.tach_pulses += 1

    def set_speed(self, percent):
        percent = max(0, min(100, percent))
        duty_cycle = int((percent / 100.0) * PWM_PERIOD_NS)
        try:
            with open(f"{PWM_FULL_PATH}/duty_cycle", "w") as f: f.write(str(duty_cycle))
        except Exception: pass
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
        if volts >= 13.2 and amps >= -0.5: self.soc = 1.0
        return self.soc * 100.0

    def save_state(self):
        try:
            with open(SOC_STATE_FILE, "w") as f:
                json.dump({"soc_fraction": self.soc, "timestamp": time.time()}, f)
        except Exception: pass

    def load_state(self):
        try:
            if os.path.exists(SOC_STATE_FILE):
                with open(SOC_STATE_FILE, "r") as f:
                    self.soc = json.load(f).get("soc_fraction", 1.0)
        except Exception: pass

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

def get_witty_data():
    """
    Native Python I2C implementation mapping directly to Witty Pi 4 utilities.sh
    """
    data = {"vin": 0.0, "vout": 0.0, "iout": 0.0, "temp_c": 0.0}
    try:
        import smbus
        bus = smbus.SMBus(1)
        
        # Witty Pi 4 I2C Address
        ADDR = 0x08
        
        # Voltages and Current (Registers 1-6)
        vin_i = bus.read_byte_data(ADDR, 1)
        vin_d = bus.read_byte_data(ADDR, 2)
        data["vin"] = round(vin_i + (vin_d / 100.0), 2)
        
        vout_i = bus.read_byte_data(ADDR, 3)
        vout_d = bus.read_byte_data(ADDR, 4)
        data["vout"] = round(vout_i + (vout_d / 100.0), 2)
        
        iout_i = bus.read_byte_data(ADDR, 5)
        iout_d = bus.read_byte_data(ADDR, 6)
        data["iout"] = round(iout_i + (iout_d / 100.0), 2)
        
        # Temperature (Register 50 / 0x32)
        # Replicating utilities.sh bitwise operations: (((data&0xFF)<<8)|((data&0xFF00)>>8))>>5
        temp_bytes = bus.read_i2c_block_data(ADDR, 50, 2)
        
        # Reconstruct the 16-bit word natively
        t_data = (temp_bytes[0] << 8) | temp_bytes[1]
        t_data = t_data >> 5
        
        if t_data >= 0x400:
            t_data = (t_data & 0x3FF) - 1024
            
        data["temp_c"] = round(t_data * 0.125, 1)
        
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
        {"id": "w_vout", "name": "Pi Supply (Witty)", "cls": "voltage", "unit": "V", "tpl": "{{ value_json.witty_vout }}"},
        {"id": "w_iout", "name": "Pi Current (Witty)", "cls": "current", "unit": "A", "tpl": "{{ value_json.witty_iout }}"},
        {"id": "curr", "name": "Battery Current", "cls": "current", "unit": "A", "tpl": "{{ value_json.current_amps }}"},
        {"id": "soc", "name": "Battery SoC", "cls": "battery", "unit": "%", "tpl": "{{ value_json.soc_percent | round(1) }}"},
        {"id": "cpu_t", "name": "Pi CPU Temp", "unit": "°C", "tpl": "{{ value_json.cpu_temp_c }}", "icon": "mdi:thermometer"},
        {"id": "w_t", "name": "Witty Hat Temp", "unit": "°C", "tpl": "{{ value_json.witty_temp_c }}", "icon": "mdi:thermometer"},
        {"id": "cpu_u", "name": "Pi CPU Usage", "unit": "%", "tpl": "{{ value_json.cpu_usage_pct }}", "icon": "mdi:cpu-64-bit"},
        {"id": "fan_s", "name": "Fan Duty Cycle", "unit": "%", "tpl": "{{ value_json.fan_speed_pct }}", "icon": "mdi:fan"},
        {"id": "fan_rpm", "name": "Fan Speed (RPM)", "unit": "RPM", "tpl": "{{ value_json.fan_rpm }}", "icon": "mdi:fan-speed"},
        {"id": "awake", "name": "Truck Power Status", "cmp": "binary_sensor", "cls": "power", "tpl": "{{ 'ON' if value_json.truck_awake else 'OFF' }}"}
    ]
    for s in sensors:
        cmp = s.get("cmp", "sensor")
        topic = f"homeassistant/{cmp}/truck_v8/{s['id']}/config"
        payload = {
            "name": s['name'], "state_topic": f"{BASE_TOPIC}/system", 
            "value_template": s['tpl'], "unique_id": f"truck_v8_{s['id']}", 
            "device": device, "unit_of_measurement": s.get("unit")
        }
        if "cls" in s: payload["device_class"] = s["cls"]
        if "icon" in s: payload["icon"] = s["icon"]
        client.publish(topic, json.dumps(payload), retain=True)

def main():
    init_hardware()
    fan = TruckFanController(FAN_TACH_GPIO)
    tracker = BatteryTracker(BATTERY_CAPACITY_AH)
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
            
            # Read cleanly every loop now that native smbus is used
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
                if chan_main_v: main_v = round(chan_main_v.voltage * DIVIDER_FACTOR, 2)
                if chan_ign_v: ign_v = round(chan_ign_v.voltage * DIVIDER_FACTOR, 2)
                if chan_curr_vout and chan_curr_vref:
                    raw_diff = chan_curr_vout.voltage - chan_curr_vref.voltage
                    amps = round((raw_diff - CURRENT_ZERO_OFFSET_V) / CURRENT_SENSITIVITY_V_PER_A, 2)
            except Exception: pass

            is_awake = ign_v > AWAKE_THRESHOLD_V

            dt = now - last_time
            last_time = now
            soc = tracker.update(amps, main_v, dt)

            payload = {
                "battery_voltage": main_v,
                "ign_voltage": ign_v,
                "witty_vin": witty_cache["vin"],
                "witty_vout": witty_cache["vout"],
                "witty_iout": witty_cache["iout"],
                "current_amps": amps,
                "soc_percent": soc,
                "cpu_temp_c": cpu_c,
                "witty_temp_c": witty_cache["temp_c"],
                "fan_speed_pct": current_fan_speed,
                "fan_rpm": current_fan_rpm,
                "truck_awake": is_awake,
                "cpu_usage_pct": psutil.cpu_percent() if psutil else 0
            }

            client.publish(f"{BASE_TOPIC}/system", json.dumps(payload))
            if int(now) % 300 < 2: tracker.save_state()
            time.sleep(1)

        except Exception as e:
            logging.critical(f"CRASH IN MAIN LOOP: {e}")
            logging.critical(traceback.format_exc())
            time.sleep(5)

if __name__ == "__main__": main()
