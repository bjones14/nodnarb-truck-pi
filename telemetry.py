import time
import json
import psutil
import paho.mqtt.client as mqtt
import subprocess
import os

# Load Configuration
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
with open(CONFIG_PATH, 'r') as f:
    config = json.load(f)

# --- CONFIGURATION FROM FILE ---
BROKER_IP = config['mqtt']['broker']
BROKER_PORT = config['mqtt']['port']
MQTT_USER = config['mqtt']['user']
MQTT_PASS = config['mqtt']['pass']
BASE_TOPIC = "truck/pi"
DASHCAM_PATH = config['paths']['dashcam_mount']

def get_cpu_temp():
    """Reads temperature from system file (extremely low overhead)."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            return round(int(f.read()) / 1000.0, 1)
    except:
        return 0.0

def get_service_status(service_name):
    """Fast exit-code based check for service status."""
    try:
        cmd = ["systemctl", "is-active", service_name]
        status = subprocess.check_output(cmd, encoding='utf-8').strip()
        return 1 if status == "active" else 0
    except:
        return 0

def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print("Telemetry successfully connected to MQTT broker.")
    else:
        print(f"Telemetry connection failed with code: {reason_code}")

def main():
    print(f"Starting Optimized Telemetry Node. Connecting to {BROKER_IP}...")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.username_pw_set(MQTT_USER, MQTT_PASS)

    connected = False
    while not connected:
        try:
            client.connect(BROKER_IP, BROKER_PORT, 60)
            client.loop_start()
            connected = True
        except:
            time.sleep(10)

    # Prime psutil
    psutil.cpu_percent(interval=None)

    while True:
        try:
            system_data = {
                "cpu_usage_pct": psutil.cpu_percent(interval=None),
                "cpu_temp_c": get_cpu_temp(),
                "ram_usage_pct": psutil.virtual_memory().percent,
                "disk_root_free_gb": round(psutil.disk_usage('/').free / (1024**3), 2),
                "dashcam_service_ok": get_service_status("dashcam.service")
            }

            try:
                system_data["disk_usb_free_gb"] = round(psutil.disk_usage(DASHCAM_PATH).free / (1024**3), 2)
            except:
                system_data["disk_usb_free_gb"] = 0.0

            client.publish(f"{BASE_TOPIC}/system", json.dumps(system_data), qos=1)
            client.publish(f"{BASE_TOPIC}/heartbeat", str(int(time.time())), qos=1)

            time.sleep(5)

        except Exception:
            time.sleep(10)

if __name__ == "__main__":
    main()
