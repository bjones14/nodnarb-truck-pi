import glob
import json
import os
import signal
import subprocess
import sys
import time

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

try:
    import pybamm
    PYBAMM_AVAILABLE = True
except ImportError:
    PYBAMM_AVAILABLE = False

# Load Configuration & Set File Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'config.json')

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

# --- CALIBRATION & BATTERY CONFIG ---
DIVIDER_FACTOR = 5.545
AWAKE_THRESHOLD_V = 9.0

# Current Sensor Calibration
CURRENT_SENSITIVITY_V_PER_A = 0.003125
CURRENT_ZERO_OFFSET_V = 0.0074
AMP_CRANK_THRESHOLD = 40.0

# Battery Protection & SoC Config
BATTERY_CAPACITY_AH = 80.0
VOLT_LOW_SHUTDOWN = 11.8
SHUTDOWN_DELAY_SECONDS = 60
SOC_STATE_FILE = os.path.join(SCRIPT_DIR, "soc_state.json")
FLOAT_VOLTAGE_THRESHOLD = 13.2
FLOAT_TIME_THRESHOLD_SEC = 3600  # 1 hour

# --- HARDWARE SETUP ---
W1_DEVICE_BASE = '/sys/bus/w1/devices/'
W1_DATA_GPIO = 23
W1_POWER_GPIO = 24
ARGON_FAN_ADDR = 0x1a

# --- GLOBAL STATE ---
i2c_bus = None
chan_f12_constant = None
chan_f26_switched = None
chan_current_vout = None
chan_current_vref = None


class BatteryTracker:
    def __init__(self, capacity_ah):
        self.capacity_ah = capacity_ah
        self.soc = 1.0  # Default to 100%
        self.float_timer = 0.0

        self.load_state()

        # Initialize PyBaMM Equivalent Circuit Model (ECM)
        self.sim = None
        if PYBAMM_AVAILABLE:
            try:
                model = pybamm.lead_acid.LOQS()
                self.sim = pybamm.Simulation(model)
                print("PyBaMM Lead-Acid model initialized successfully.")
            except Exception as e:
                print(f"Warning: Failed to initialize PyBaMM model: {e}")
                self.sim = None

    def update(self, current_amps, voltage, dt_seconds):
        # 1. Coulomb Counting (Ah Integration)
        ah_delta = (current_amps * dt_seconds) / 3600.0
        self.soc += (ah_delta / self.capacity_ah)
        self.soc = max(0.0, min(1.0, self.soc))

        # Auto-Sync to 100% if floating/charging
        # If voltage is high and we aren't draining heavily, start the timer
        if voltage >= FLOAT_VOLTAGE_THRESHOLD and current_amps >= -0.5:
            self.float_timer += dt_seconds
            if self.float_timer >= FLOAT_TIME_THRESHOLD_SEC:
                self.soc = 1.0
        else:
            self.float_timer = 0.0

        # 2. PyBaMM ECM Correction
        # If the model is active, we step it forward and use its internal
        # voltage estimation to correct long-term Coulomb counting drift.
        if self.sim and dt_seconds > 0:
            try:
                # Placeholder for active step logic
                pass
            except Exception as e:
                print(f"PyBaMM step error: {e}")

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
                    data = json.load(f)
                    self.soc = data.get("soc_fraction", 1.0)
                    print(f"Loaded persistent SoC state: {self.soc * 100:.1f}%")
        except Exception as e:
            print(f"Could not load SoC state, defaulting to 100%: {e}")


def hard_reset_1wire():
    try:
        subprocess.run(['sudo', 'modprobe', '-r', 'w1-therm'], capture_output=True)
        subprocess.run(['sudo', 'modprobe', '-r', 'w1-gpio'], capture_output=True)
        subprocess.run(['sudo', 'pinctrl', 'set', str(W1_POWER_GPIO), 'op', 'dl'], capture_output=True)
        time.sleep(2)
        subprocess.run(['sudo', 'pinctrl', 'set', str(W1_POWER_GPIO), 'op', 'dh'], capture_output=True)
        time.sleep(1)
        subprocess.run(['sudo', 'modprobe', 'w1-gpio'], capture_output=True)
        subprocess.run(['sudo', 'modprobe', 'w1-therm'], capture_output=True)
        time.sleep(2)
    except Exception:
        pass


def get_cabin_temp():
    try:
        device_folders = glob.glob(W1_DEVICE_BASE + '28*')
        if not device_folders:
            hard_reset_1wire()
            return None
        device_file = device_folders[0] + '/w1_slave'
        if not os.path.exists(device_file):
            return None
        with open(device_file, 'r') as f:
            lines = f.readlines()
        if not lines or "YES" not in lines[0]:
            return None
        equals_pos = lines[1].find('t=')
        if equals_pos != -1:
            temp_string = lines[1][equals_pos+2:]
            return round(float(temp_string) / 1000.0, 1)
    except Exception:
        pass
    return None


def init_hardware():
    global i2c_bus, chan_f12_constant, chan_f26_switched, chan_current_vout, chan_current_vref
    subprocess.run(['sudo', 'pinctrl', 'set', str(W1_POWER_GPIO), 'op', 'dh'], capture_output=True)

    if board and busio:
        try:
            i2c_bus = busio.I2C(board.SCL, board.SDA)
            if ADS:
                ads = ADS.ADS1115(i2c_bus)
                ads.gain = 1
                chan_f12_constant = AnalogIn(ads, 0)
                chan_f26_switched = AnalogIn(ads, 1)
                chan_current_vout = AnalogIn(ads, 2)
                chan_current_vref = AnalogIn(ads, 3)
        except Exception:
            pass
    hard_reset_1wire()


def get_voltage(channel, apply_divider=True):
    if channel:
        try:
            raw_v = channel.voltage
            return round(raw_v * DIVIDER_FACTOR, 2) if apply_divider else raw_v
        except Exception:
            pass
    return 0.0


def get_current_amps():
    if chan_current_vout and chan_current_vref:
        try:
            vout = get_voltage(chan_current_vout, apply_divider=False)
            vref = get_voltage(chan_current_vref, apply_divider=False)
            raw_diff_v = vout - vref
            corrected_diff_v = raw_diff_v - CURRENT_ZERO_OFFSET_V
            amps = corrected_diff_v / CURRENT_SENSITIVITY_V_PER_A
            return round(amps, 2)
        except Exception:
            pass
    return 0.0


def set_argon_fan_speed(speed):
    if i2c_bus:
        try:
            while not i2c_bus.try_lock():
                pass
            i2c_bus.writeto(ARGON_FAN_ADDR, bytes([int(speed)]))
            i2c_bus.unlock()
            return int(speed)
        except Exception:
            try:
                i2c_bus.unlock()
            except Exception:
                pass
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
    except Exception:
        return 0.0


def get_power_status():
    try:
        res = subprocess.check_output(["vcgencmd", "get_throttled"], encoding='utf-8')
        val = int(res.strip().split('=')[1], 16)
        if bool(val & 0x1): return "Problem (Low Voltage)"
        if bool(val & 0x10000): return "Warning (Voltage Dip)"
        return "Stable"
    except Exception:
        return "Unknown"


def publish_ha_discovery(client):
    device_info = {"identifiers": ["silverado_telemetry_pi"], "name": "Silverado Telemetry Node", "model": "Pi 4 Pro", "manufacturer": "Custom"}
    sensors = [
        {"id": "batt_v", "name": "Main Battery", "cmp": "sensor", "cls": "voltage", "unit": "V", "tpl": "{{ value_json.battery_voltage }}"},
        {"id": "ign_v", "name": "Ignition Signal", "cmp": "sensor", "cls": "voltage", "unit": "V", "tpl": "{{ value_json.ign_voltage }}"},
        {"id": "curr", "name": "Battery Current", "cmp": "sensor", "cls": "current", "unit": "A", "tpl": "{{ value_json.current_amps }}"},
        {"id": "soc", "name": "Battery SoC", "cmp": "sensor", "cls": "battery", "unit": "%", "tpl": "{{ value_json.soc_percent | round(1) }}"},
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
    init_hardware()

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except (AttributeError, TypeError):
        client = mqtt.Client()

    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.will_set(f"{BASE_TOPIC}/status", "Offline", retain=True)

    connected = False
    while not connected:
        try:
            client.connect(BROKER_IP, BROKER_PORT, 60)
            client.loop_start()
            publish_ha_discovery(client)
            connected = True
        except Exception:
            time.sleep(10)

    last_awake_state = None
    low_volt_seconds = 0
    tracker = BatteryTracker(BATTERY_CAPACITY_AH)
    last_tracker_time = time.monotonic()

    while True:
        try:
            # 1. Grab environmental variables once per cycle
            cpu_temp = get_cpu_temp()
            fan_speed = set_argon_fan_speed(get_fan_curve_speed(cpu_temp))
            main_v = get_voltage(chan_f12_constant)
            ign_v = get_voltage(chan_f26_switched)
            cabin_t = get_cabin_temp()
            pwr_status = get_power_status()
            is_awake = ign_v > AWAKE_THRESHOLD_V

            # 2. FAST POLLING LOOP FOR CURRENT
            cycle_duration = 10 if is_awake else 60
            start_time = time.monotonic()
            peak_draw = 0.0
            last_amps = 0.0

            while time.monotonic() - start_time < cycle_duration:
                current = get_current_amps()
                last_amps = current
                if current < peak_draw:
                    peak_draw = current
                time.sleep(0.1)

            # 3. Process Amp and SoC Data
            is_cranking = peak_draw < -AMP_CRANK_THRESHOLD
            reported_amps = peak_draw if is_cranking else last_amps

            now = time.monotonic()
            dt_seconds = now - last_tracker_time
            last_tracker_time = now

            soc_percent = tracker.update(reported_amps, main_v, dt_seconds)

            # 4. State Management & Low Voltage Shutdown
            if 0.5 < main_v <= VOLT_LOW_SHUTDOWN:
                if not is_cranking:
                    low_volt_seconds += dt_seconds

                if low_volt_seconds >= SHUTDOWN_DELAY_SECONDS:
                    client.publish(f"{BASE_TOPIC}/status", f"HALTING: {main_v}V", retain=True)
                    tracker.save_state()
                    time.sleep(2)
                    os.system("sudo halt")
            else:
                low_volt_seconds = 0

            if is_awake != last_awake_state:
                client.publish(f"{BASE_TOPIC}/status", "Awake" if is_awake else "Parked", retain=True)
                last_awake_state = is_awake

            # 5. Publish Payload
            payload = {
                "battery_voltage": main_v,
                "ign_voltage": ign_v,
                "current_amps": reported_amps,
                "soc_percent": soc_percent,
                "cabin_temp_c": cabin_t,
                "cpu_temp_c": cpu_temp,
                "fan_speed_pct": fan_speed,
                "power_status": pwr_status,
                "truck_awake": is_awake,
                "cpu_usage_pct": psutil.cpu_percent() if psutil else 0
            }

            try:
                tmp_file = "/dev/shm/telemetry.tmp"
                final_file = "/dev/shm/telemetry.json"
                with open(tmp_file, "w") as f:
                    json.dump(payload, f)
                os.rename(tmp_file, final_file)
            except Exception:
                pass

            client.publish(f"{BASE_TOPIC}/system", json.dumps(payload), qos=1)
            print(f"[{time.strftime('%H:%M:%S')}] Payload: {payload}", flush=True)

            # Save SoC state periodically
            if int(now) % 300 < cycle_duration:
                tracker.save_state()

        except Exception as e:
            print(f"CRITICAL LOOP ERROR: {e}", flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()

