import time
import json
import psutil
import paho.mqtt.client as mqtt
import subprocess

# --- CONFIGURATION ---
BROKER_IP = "homeassistant"
BROKER_PORT = 1883
BASE_TOPIC = "truck/pi"
MQTT_USER = "truck"
MQTT_PASS = "truck"

def get_cpu_temp():
    """Reads temperature from system file (extremely low overhead)."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            return round(int(f.read()) / 1000.0, 1)
    except:
        return 0.0

def get_service_status(service_name):
    """
    Checks if a systemd service is active.
    Uses 'is-active' which is a fast exit-code check.
    """
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

    # Initialize MQTT Client using v2 API
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
            # Wait longer between reconnect attempts to save CPU during broker outages
            time.sleep(10)

    # Prime psutil with an initial call to set the baseline for delta calculations
    psutil.cpu_percent(interval=None)

    while True:
        try:
            # We use interval=None to calculate the CPU delta since the last loop iteration.
            # This is non-blocking and virtually zero-cost compared to interval=0.1.
            system_data = {
                "cpu_usage_pct": psutil.cpu_percent(interval=None),
                "cpu_temp_c": get_cpu_temp(),
                "ram_usage_pct": psutil.virtual_memory().percent,
                "disk_root_free_gb": round(psutil.disk_usage('/').free / (1024**3), 2),
                "dashcam_service_ok": get_service_status("dashcam.service")
            }

            # Optional: Check dashcam USB storage
            try:
                # We check the disk usage here rather than a separate thread to keep logic linear
                system_data["disk_usb_free_gb"] = round(psutil.disk_usage('/mnt/dashcam').free / (1024**3), 2)
            except:
                system_data["disk_usb_free_gb"] = 0.0

            # Publish the JSON Payload
            client.publish(f"{BASE_TOPIC}/system", json.dumps(system_data), qos=1)

            # Publish a Unix timestamp heartbeat
            client.publish(f"{BASE_TOPIC}/heartbeat", str(int(time.time())), qos=1)

            # Sleep for 5 seconds. This frequency is a good balance between
            # real-time monitoring and minimizing system overhead.
            time.sleep(5)

        except Exception as e:
            # If a major error occurs, wait before retrying to prevent rapid log spam
            time.sleep(10)

if __name__ == "__main__":
    main()
