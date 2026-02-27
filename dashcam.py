import os
import time
import threading
import datetime
import subprocess
import psutil
import json
import argparse
import shutil
from flask import Flask, send_from_directory

app = Flask(__name__)

# --- PARSE COMMAND LINE ARGS ---
parser = argparse.ArgumentParser(description="Silverado Dashcam Service")
parser.add_argument('--ignore-parked', action='store_true', help="Force continuous MP4 recording to disk, ignoring WiFi.")
parser.add_argument('--chunk-seconds', type=int, default=900, help="Duration of video chunk in seconds (Default 15m).")
parser.add_argument('--hw-buffer', type=int, default=2, help="Hardware release buffer time in seconds.")
parser.add_argument('--compress', action='store_true', help="Use Pi 4 hardware encoder to shrink file size.")
parser.add_argument('--bitrate', type=str, default="3.5M", help="Target bitrate for compression (e.g., 2M, 4M, 500K).")
args, unknown = parser.parse_known_args()

IGNORE_PARKED = args.ignore_parked
CHUNK_SECONDS = args.chunk_seconds
HW_BUFFER_SECONDS = args.hw_buffer
USE_COMPRESSION = args.compress
TARGET_BITRATE = args.bitrate

# Load Configuration
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
try:
    with open(CONFIG_PATH, 'r') as f:
        config = json.load(f)
except Exception:
    config = {
        "paths": {"dashcam_mount": "/mnt/dashcam", "ram_disk": "/mnt/ramdisk"},
        "wifi": {"prefix": "Silverado_Guest"}
    }

DISK_PATH = config['paths']['dashcam_mount']
RAM_DISK = config['paths']['ram_disk']
WIFI_PREFIX = config['wifi'].get('prefix', 'Silverado_Guest')
MAX_DISK_USAGE_PERCENT = 90

# Global State
garage_mode_active = False

def check_wifi_geofence():
    global garage_mode_active
    if IGNORE_PARKED:
        garage_mode_active = False
        return
    while True:
        try:
            cmd = "nmcli -t -f ACTIVE,SSID dev wifi | grep '^yes' | cut -d':' -f2"
            current_ssid = subprocess.check_output(cmd, shell=True, encoding='utf-8').strip()
            is_home = current_ssid.startswith(WIFI_PREFIX)
            if is_home != garage_mode_active:
                garage_mode_active = is_home
        except Exception: pass
        time.sleep(60)

threading.Thread(target=check_wifi_geofence, daemon=True).start()

def is_camera_present():
    return os.path.exists("/dev/video0")

def cleanup_ram_disk():
    if os.path.exists(RAM_DISK):
        for f in os.listdir(RAM_DISK):
            if f.startswith('stream') or f.endswith('.tmp_srt'):
                try: os.remove(os.path.join(RAM_DISK, f))
                except: pass

def cleanup_old_footage():
    try:
        usage = psutil.disk_usage(DISK_PATH)
        if usage.percent > MAX_DISK_USAGE_PERCENT:
            folders = sorted([f for f in os.listdir(DISK_PATH) if os.path.isdir(os.path.join(DISK_PATH, f))])
            if folders:
                shutil.rmtree(os.path.join(DISK_PATH, folders[0]))
    except Exception: pass

def record_loop():
    cleanup_ram_disk()
    while True:
        if not is_camera_present():
            time.sleep(5.0)
            continue

        cleanup_old_footage()
        now = datetime.datetime.now()
        daily_path = os.path.join(DISK_PATH, now.strftime("%Y-%m-%d"))
        os.makedirs(daily_path, exist_ok=True)

        ts = now.strftime("%Y%m%d_%H%M%S")
        mp4_file = os.path.join(daily_path, f"Silverado_{ts}.mp4")
        final_srt = os.path.join(daily_path, f"Silverado_{ts}.srt")

        # --- VIDEO ENGINE SELECTION ---
        # Option A: 'copy' (Zero CPU, size is dictated by camera)
        # Option B: 'h264_v4l2m2m' (Uses Pi 4 chip to force a specific bitrate)
        if USE_COMPRESSION:
            v_codec = ["-c:v", "h264_v4l2m2m", "-b:v", TARGET_BITRATE, "-num_capture_buffers", "32"]
        else:
            v_codec = ["-c:v", "copy"]

        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "v4l2", "-input_format", "h264", "-video_size", "1920x1080", "-framerate", "30",
            "-t", str(CHUNK_SECONDS), "-i", "/dev/video0"
        ] + v_codec + [
            "-max_muxing_queue_size", "9999",
            "-movflags", "+frag_keyframe+empty_moov", mp4_file,
            "-c:v", "copy", "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
            "-hls_flags", "delete_segments", os.path.join(RAM_DISK, "stream.m3u8")
        ]

        threading.Thread(target=generate_srt, args=(final_srt,), daemon=True).start()

        try:
            process = subprocess.Popen(ffmpeg_cmd)
            while process.poll() is None:
                if not is_camera_present():
                    process.terminate()
                    break
                time.sleep(2)
            process.wait()
        except Exception: pass
        time.sleep(HW_BUFFER_SECONDS)

def get_telemetry():
    try:
        with open("/dev/shm/telemetry.json", "r") as f:
            d = json.load(f)
        temp = f"{(d.get('cabin_temp_c', 0)*9/5)+32:.0f}F" if d.get("cabin_temp_c") else "N/A"
        return temp, f"{d.get('battery_voltage',0):.1f}", f"{d.get('current_amps',0):.1f}"
    except: return "N/A", "--.-", "--.-"

def generate_srt(srt_file):
    idx = 1
    start = time.monotonic()
    try:
        with open(srt_file, "w") as f:
            while idx <= CHUNK_SECONDS:
                elapsed = time.monotonic() - start
                ts_now = datetime.datetime.now().strftime('%H:%M:%S')
                t, v, a = get_telemetry()
                f.write(f"{idx}\n{str(datetime.timedelta(seconds=int(elapsed)))},000 --> {str(datetime.timedelta(seconds=int(elapsed+1)))},000\n")
                f.write(f"{ts_now} | Bat: {v}V {a}A | Cabin: {t}\n\n")
                f.flush()
                idx += 1
                time.sleep(1.0)
    except: pass

@app.route('/stream.m3u8')
def stream_m3u8(): return send_from_directory(RAM_DISK, 'stream.m3u8')

@app.route('/<path:filename>')
def stream_ts(filename):
    if filename.endswith('.ts'): return send_from_directory(RAM_DISK, filename)
    return "Not found", 404

if __name__ == "__main__":
    threading.Thread(target=record_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)

