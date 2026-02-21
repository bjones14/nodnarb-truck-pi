import os
import time
import threading
import datetime
import subprocess
import psutil
import json
import paho.mqtt.client as mqtt
from flask import Flask, send_from_directory

app = Flask(__name__)

# Load Configuration
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
with open(CONFIG_PATH, 'r') as f:
    config = json.load(f)

# --- CONFIGURATION FROM FILE ---
DISK_PATH = config['paths']['dashcam_mount']
RAM_DISK = config['paths']['ram_disk']
WIFI_PREFIX = config['wifi']['prefix']
BROKER_IP = config['mqtt']['broker']
MQTT_USER = config['mqtt']['user']
MQTT_PASS = config['mqtt']['pass']

MAX_DISK_USAGE_PERCENT = 90
CHUNK_SECONDS = 300
STATUS_TOPIC = "truck/dashcam/status"

# Global State
garage_mode_active = False

def report_status(msg):
    """Pushes mode changes to MQTT using a background thread."""
    def _do_report():
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            client.username_pw_set(MQTT_USER, MQTT_PASS)
            client.connect(BROKER_IP, 1883, 5)
            client.publish(STATUS_TOPIC, msg, retain=True)
            client.disconnect()
        except: pass
    threading.Thread(target=_do_report, daemon=True).start()

def check_wifi_geofence():
    """Polls for home networks every 60s to minimize CPU impact."""
    global garage_mode_active
    while True:
        try:
            cmd = "nmcli -t -f ACTIVE,SSID dev wifi | grep '^yes' | cut -d':' -f2"
            current_ssid = subprocess.check_output(cmd, shell=True, encoding='utf-8').strip()

            is_home = current_ssid.startswith(WIFI_PREFIX)

            if is_home and not garage_mode_active:
                garage_mode_active = True
                report_status(f"Garage Mode: {current_ssid}")
            elif not is_home and garage_mode_active:
                garage_mode_active = False
                report_status("Road Mode: Recording Enabled")
        except:
            if garage_mode_active:
                garage_mode_active = False
        time.sleep(60)

threading.Thread(target=check_wifi_geofence, daemon=True).start()

def disk_cleaner():
    """Cleanup old clips every 10 mins."""
    while True:
        try:
            stat = os.statvfs(DISK_PATH)
            percent_used = 100 * (1 - (stat.f_bavail / stat.f_blocks))
            while percent_used > MAX_DISK_USAGE_PERCENT:
                files = [os.path.join(DISK_PATH, f) for f in os.listdir(DISK_PATH) if f.endswith(('.mp4', '.srt'))]
                if not files: break
                oldest_file = min(files, key=os.path.getctime)
                base_name = os.path.splitext(oldest_file)[0]
                for ext in ['.mp4', '.srt']:
                    f = base_name + ext
                    if os.path.exists(f): os.remove(f)
                stat = os.statvfs(DISK_PATH)
                percent_used = 100 * (1 - (stat.f_bavail / stat.f_blocks))
        except: pass
        time.sleep(600)

def record_loop():
    """Manages the FFmpeg process with low-frequency polling."""
    for f in os.listdir(RAM_DISK):
        if f.startswith('stream') and (f.endswith('.m3u8') or f.endswith('.ts')):
            try: os.remove(os.path.join(RAM_DISK, f))
            except: pass

    while True:
        if garage_mode_active:
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-f", "v4l2", "-input_format", "h264", "-video_size", "1920x1080", "-framerate", "30",
                "-i", "/dev/video0",
                "-c:v", "copy", "-f", "hls", "-hls_time", "2", "-hls_list_size", "5", "-hls_flags", "delete_segments",
                os.path.join(RAM_DISK, "stream.m3u8")
            ]
        else:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            base_filename = os.path.join(DISK_PATH, f"SilverADO_{ts}")
            mp4_file = f"{base_filename}.mp4"
            srt_file = f"{base_filename}.srt"

            ffmpeg_cmd = [
                "ffmpeg", "-y", "-f", "v4l2", "-input_format", "h264", "-video_size", "1920x1080", "-framerate", "30",
                "-t", str(CHUNK_SECONDS), "-i", "/dev/video0",
                "-c:v", "copy", mp4_file,
                "-c:v", "copy", "-f", "hls", "-hls_time", "2", "-hls_list_size", "5", "-hls_flags", "delete_segments",
                os.path.join(RAM_DISK, "stream.m3u8")
            ]
            threading.Thread(target=generate_srt, args=(srt_file,), daemon=True).start()

        process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        while process.poll() is None:
            # Check for mode mismatch
            is_cmd_recording = ("mp4" in str(ffmpeg_cmd))
            if garage_mode_active == is_cmd_recording:
                process.terminate()
                break
            time.sleep(10)

        process.wait()

def generate_srt(srt_file):
    """Generates telemetry subtitles with non-blocking CPU calls."""
    srt_index = 1
    start_time = time.time()
    psutil.cpu_percent(interval=None) 

    try:
        with open(srt_file, "w") as f:
            while srt_index <= CHUNK_SECONDS and not garage_mode_active:
                elapsed = time.time() - start_time
                current_time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cpu = psutil.cpu_percent(interval=None)

                start_ts = str(datetime.timedelta(seconds=int(elapsed))) + ",000"
                end_ts = str(datetime.timedelta(seconds=int(elapsed + 1))) + ",000"

                f.write(f"{srt_index}\n{start_ts} --> {end_ts}\n")
                f.write(f"SILVERADO | {current_time_str} | CPU: {cpu}% | MODE: ROAD\n\n")
                f.flush()

                srt_index += 1
                time.sleep(1.0)
    except: pass

@app.route('/stream.m3u8')
def stream_m3u8():
    return send_from_directory(RAM_DISK, 'stream.m3u8')

@app.route('/<path:filename>')
def stream_ts(filename):
    if filename.endswith('.ts'):
        return send_from_directory(RAM_DISK, filename)
    return "Not found", 404

if __name__ == "__main__":
    threading.Thread(target=disk_cleaner, daemon=True).start()
    threading.Thread(target=record_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, threaded=True)
