import time
import json
import subprocess
import os
import sys
import glob

# Prevent bytecode writes in Read-Only mode
sys.dont_write_bytecode = True

# --- ROBUST LIBRARY LOADING ---
psutil = None
mqtt = None
ADS = None
AnalogIn = None
board = None
busio = None

try:
    import psutil
except ImportError:
    pass

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("CRITICAL: 'paho-mqtt' not found.")
    sys.exit(1)

try:
    import board
    import busio
    import adafruit_ads1x15.ads1115 as ADS
    from adafruit_ads1x15.analog_in import AnalogIn
except ImportError:
    pass

# Load Configuration
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
try:
    with open(CONFIG_PATH, 'r') as f:
        config = json.load(f)
except Exception:
    config = {"mqtt": {"broker": "homeassistant", "port": 1883, "user": "truck", "pass": "truck"}}

# --- MQTT CONFIG ---
BROKER_IP = config.get('mqtt', {}).get('broker', 'homeassistant')
BROKER_PORT = config.get('mqtt', {}).get('port', 1883)
MQTT_USER = config.get('mqtt', {}).get('user', 'truck')
MQTT_PASS = config.get('mqtt', {}).get('pass', 'truck')
BASE_TOPIC = "truck/pi"

# --- CALIBRATION ---
DIVIDER_FACTOR = 5.545
VOLT_CRITICAL_HALT = 11.2
VOLT_LOW_WARNING = 11.8
AWAKE_THRESHOLD_V = 9.0

# CURRENT SENSOR CALIBRATION (Absolute Hardware Zero)
# Updated to 0.5847V based on image_8c502f.png (sensor disconnected)
CURRENT_ZERO_VOLTAGE = 0.5847
CURRENT_SENSITIVITY_V_PER_A = 0.033
AMP_CRANK_THRESHOLD = 40.0

# --- HARDWARE SETUP (Pins 14, 16, 18) ---
W1_DEVICE_BASE = '/sys/bus/w1/devices/'
W1_DATA_GPIO = 23
W1_POWER_GPIO = 24
ARGON_FAN_ADDR = 0x1a

# --- GLOBAL STATE ---
i2c_bus = None
chan_f12_constant = None
chan_f26_switched = None
chan_current_sensor = None
low_volt_counter = 0

def hard_reset_1wire():
    """Toggles GPIO power and reloads modules to clear sensor latch-up."""
    try:
        subprocess.run(['sudo', 'modprobe', '-r', 'w1-therm'], capture_output=True)
        subprocess.run(['sudo', 'modprobe', '-r', 'w1-gpio'], capture_output=True)
        # Cycle power on Pin 18
        subprocess.run(['sudo', 'pinctrl', 'set', str(W1_POWER_GPIO), 'op', 'dl'], capture_output=True)
        time.sleep(2)
        subprocess.run(['sudo', 'pinctrl', 'set', str(W1_POWER_GPIO), 'op', 'dh'], capture_output=True)
        time.sleep(1)
        subprocess.run(['sudo', 'modprobe', 'w1-gpio'], capture_output=True)
        subprocess.run(['sudo', 'modprobe', 'w1-therm'], capture_output=True)
        time.sleep(2)
    except Exception: pass

def get_cabin_temp():
    try:
        device_folders = glob.glob(W1_DEVICE_BASE + '28*')
        if not device_folders:
            hard_reset_1wire()
            return None
        device_file = device_folders[0] + '/w1_slave'
        if not os.path.exists(device_file): return None
        with open(device_file, 'r') as f:
            lines = f.readlines()
        if not lines or "YES" not in lines[0]: return None
        equals_pos = lines[1].find('t=')
        if equals_pos != -1:
            temp_string = lines[1][equals_pos+2:]
            return round(float(temp_string) / 1000.0, 1)
    except Exception: pass
    return None

def init_hardware():
    global i2c_bus, chan_f12_constant, chan_f26_switched, chan_current_sensor
    # Ensure Pin 18 is providing 3.3V for the DS18B20 sensor
    subprocess.run(['sudo', 'pinctrl', 'set', str(W1_POWER_GPIO), 'op', 'dh'], capture_output=True)

    if board and busio:
        try:
            i2c_bus = busio.I2C(board.SCL, board.SDA)
            if ADS:
                ads = ADS.ADS1115(i2c_bus)
                chan_f12_constant = AnalogIn(ads, 0)
                chan_f26_switched = AnalogIn(ads, 1)
                chan_current_sensor = AnalogIn(ads, 2)
        except Exception: pass
    hard_reset_1wire()

def get_voltage(channel, apply_divider=True):
    if channel:
        try:
            raw_v = channel.voltage
            return round(raw_v * DIVIDER_FACTOR, 2) if apply_divider else raw_v
        except: pass
    return 0.0

def get_current_amps():
    if chan_current_sensor:
        try:
            sensor_v = get_voltage(chan_current_sensor, apply_divider=False)
            # Amps = (Observed - Zero) / Sensitivity
            amps = (sensor_v - CURRENT_ZERO_VOLTAGE) / CURRENT_SENSITIVITY_V_PER_A
            return round(amps, 2)
        except: pass
    return 0.0

def set_argon_fan_speed(speed):
    if i2c_bus:
        try:
            while not i2c_bus.try_lock(): pass
            i2c_bus.writeto(ARGON_FAN_ADDR, bytes([int(speed)]))
            i2c_bus.unlock()
            return int(speed)
        except:
            try: i2c_bus.unlock()
            except: pass
    return 0

def get_fan_curve_speed(temp_c):
    if temp_c < 55: return 0
    if temp_c < 60: return 30
    if temp_c < 65: return 55
    if temp_c < 70: return 80
    return 100

def get_cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            return round(int(f.read()) / 1000.0, 1)
    except: return 0.0

def get_power_status():
    try:
        res = subprocess.check_output(["vcgencmd", "get_throttled"], encoding='utf-8')
        val = int(res.strip().split('=')[1], 16)
        if bool(val & 0x1): return "Problem (Low Voltage)"
        if bool(val & 0x10000): return "Warning (Voltage Dip)"
        return "Stable"
    except: return "Unknown"

def publish_ha_discovery(client):
    device_info = {"identifiers": ["silverado_telemetry_pi"], "name": "Silverado Telemetry Node", "model": "Pi 4 Pro", "manufacturer": "Custom"}
    sensors = [
        {"id": "batt_v", "name": "Main Battery", "cmp": "sensor", "cls": "voltage", "unit": "V", "tpl": "{{ value_json.battery_voltage }}"},
        {"id": "ign_v", "name": "Ignition Signal", "cmp": "sensor", "cls": "voltage", "unit": "V", "tpl": "{{ value_json.ign_voltage }}"},
        {"id": "curr", "name": "Battery Current", "cmp": "sensor", "cls": "current", "unit": "A", "tpl": "{{ value_json.current_amps }}"},
        {"id": "cabin_t", "name": "Truck Cabin Temp", "cmp": "sensor", "cls": "temperature", "unit": "°C", "tpl": "{{ value_json.cabin_temp_c }}"},
        {"id": "cpu_t", "name": "Pi CPU Temp", "cmp": "sensor", "cls": "temperature", "unit": "°C", "tpl": "{{ value_json.cpu_temp_c }}"},
        {"id": "fan_s", "name": "Argon Fan Speed", "cmp": "sensor", "unit": "%", "tpl": "{{ value_json.fan_speed_pct }}", "icon": "mdi:fan"},
        {"id": "pwr_q", "name": "Pi Power Quality", "cmp": "sensor", "tpl": "{{ value_json.power_status }}", "icon": "mdi:lightning-bolt"},
        {"id": "awake", "name": "Truck Power Status", "cmp": "binary_sensor", "cls": "power", "tpl": "{{ 'ON' if value_json.truck_awake else 'OFF' }}"}
    ]
    for s in sensors:
        topic = f"homeassistant/{s['cmp']}/silverado_pi/{s['id']}/config"
        payload = {"name": s['name'], "state_topic": f"{BASE_TOPIC}/system", "value_template": s['tpl'], "unique_id": f"silverado_{s['id']}", "device": device_info}
        if "cls" in s: payload["device_class"] = s["cls"]
        if "unit" in s: payload["unit_of_measurement"] = s["unit"]
        if "icon" in s: payload["icon"] = s["icon"]
        client.publish(topic, json.dumps(payload), retain=True)

def main():
    global low_volt_counter
    init_hardware()
    try: client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except (AttributeError, TypeError): client = mqtt.Client()
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.will_set(f"{BASE_TOPIC}/status", "Offline", retain=True)

    connected = False
    while not connected:
        try:
            client.connect(BROKER_IP, BROKER_PORT, 60)
            client.loop_start()
            publish_ha_discovery(client)
            connected = True
        except Exception: time.sleep(10)

    last_awake_state = None

    while True:
        try:
            cpu_temp = get_cpu_temp()
            fan_speed = set_argon_fan_speed(get_fan_curve_speed(cpu_temp))
            main_v = get_voltage(chan_f12_constant)
            ign_v = get_voltage(chan_f26_switched)
            amps = get_current_amps()
            cabin_t = get_cabin_temp()
            pwr_status = get_power_status()

            # Adjusted ignition logic to handle ADC noise offset
            is_awake = ign_v > AWAKE_THRESHOLD_V
            is_cranking = abs(amps) > AMP_CRANK_THRESHOLD

            if 0.5 < main_v < VOLT_CRITICAL_HALT:
                if not is_cranking: low_volt_counter += 1
                if low_volt_counter >= 5:
                    client.publish(f"{BASE_TOPIC}/status", f"HALTING: {main_v}V", retain=True)
                    time.sleep(2)
                    os.system("sudo halt")
            else: low_volt_counter = 0

            if is_awake != last_awake_state:
                os.system(f"sudo systemctl {'start' if is_awake else 'stop'} dashcam.service")
                client.publish(f"{BASE_TOPIC}/status", "Awake" if is_awake else "Parked", retain=True)
                last_awake_state = is_awake

            payload = {
                "battery_voltage": main_v,
                "ign_voltage": ign_v,
                "current_amps": amps,
                "cabin_temp_c": cabin_t,
                "cpu_temp_c": cpu_temp,
                "fan_speed_pct": fan_speed,
                "power_status": pwr_status,
                "truck_awake": is_awake,
                "cpu_usage_pct": psutil.cpu_percent() if psutil else 0
            }
            client.publish(f"{BASE_TOPIC}/system", json.dumps(payload), qos=1)
            time.sleep(10 if is_awake else 60)
        except Exception: time.sleep(10)

if __name__ == "__main__":
    main()

