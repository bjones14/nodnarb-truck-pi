import os
import time
import threading
import datetime
import subprocess
import psutil
import paho.mqtt.client as mqtt
from flask import Flask, send_from_directory

app = Flask(__name__)

# --- CONFIGURATION ---
DISK_PATH = "/mnt/dashcam"
RAM_DISK = "/dev/shm"
MAX_DISK_USAGE_PERCENT = 90
CHUNK_SECONDS = 300

# --- MQTT STATUS REPORTING ---
BROKER_IP = "homeassistant"
MQTT_USER = "truck"
MQTT_PASS = "truck"
STATUS_TOPIC = "truck/dashcam/status"

# --- GEOFENCING CONFIG ---
WIFI_PREFIX = "Waldon_"
garage_mode_active = False

# Global CPU tracker to keep overhead at near-zero
def get_cpu_reading():
    try:
        # Non-blocking call: returns usage since last call
        return psutil.cpu_percent(interval=None)
    except:
        return 0.0

def report_status(msg):
    """Pushes mode changes to MQTT with a short timeout."""
    def _do_report():
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            client.username_pw_set(MQTT_USER, MQTT_PASS)
            client.connect(BROKER_IP, 1883, 5)
            client.publish(STATUS_TOPIC, msg, retain=True)
            client.disconnect()
        except: pass
    # Run in thread to not block the main logic
    threading.Thread(target=_do_report, daemon=True).start()

def check_wifi_geofence():
    """Polls for 'Waldon_' networks every 60s (low overhead)."""
    global garage_mode_active
    while True:
        try:
            # Simple check for active SSID
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

# Start geofence monitor
threading.Thread(target=check_wifi_geofence, daemon=True).start()

# --- AUTOMATIC STORAGE MANAGER ---
def disk_cleaner():
    """Checked every 10 mins; very low impact."""
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

# --- THE PRO RECORDING ENGINE ---
def record_loop():
    """Optimized recording loop using process waiting instead of polling."""
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
        # We only poll once every 10 seconds to check for garage mode transitions
        # This significantly reduces CPU jitter
        while process.poll() is None:
            current_is_recording = (not garage_mode_active)
            is_cmd_recording = ("mp4" in ffmpeg_cmd[ffmpeg_cmd.index("-c:v")+1:ffmpeg_cmd.index("-c:v")+3])

            if garage_mode_active and is_cmd_recording:
                process.terminate()
                break
            elif not garage_mode_active and not is_cmd_recording:
                process.terminate()
                break
            time.sleep(10)

        process.wait()

def generate_srt(srt_file):
    """SRT generator with zero busy-waiting."""
    srt_index = 1
    start_time = time.time()
    # Initial call to psutil to prime the delta
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

# --- HLS WEB SERVER ---
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
