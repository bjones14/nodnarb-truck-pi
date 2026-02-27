import os
import time
import threading
import datetime
import subprocess
import psutil
import json
import argparse
from flask import Flask, send_from_directory

app = Flask(__name__)

# --- PARSE COMMAND LINE ARGS ---
parser = argparse.ArgumentParser(description="Silverado Dashcam Service")
parser.add_argument('--ignore-parked', action='store_true', help="Force continuous MP4 recording to disk, ignoring WiFi.")
parser.add_argument('--chunk-seconds', type=int, default=900, help="Duration of each video chunk in seconds (default: 900).")
parser.add_argument('--hw-buffer', type=int, default=2, help="Hardware release buffer time in seconds (default: 2).")
args, unknown = parser.parse_known_args()

IGNORE_PARKED = args.ignore_parked
CHUNK_SECONDS = args.chunk_seconds
HW_BUFFER_SECONDS = args.hw_buffer

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
STATUS_TOPIC = "truck/dashcam/status"

# Global State
garage_mode_active = False

def check_wifi_geofence():
    """Polls for home networks every 60s."""
    global garage_mode_active

    if IGNORE_PARKED:
        print("Flag --ignore-parked detected. Forcing ROAD MODE (saving to permanent disk).")
        garage_mode_active = False
        return

    while True:
        try:
            cmd = "nmcli -t -f ACTIVE,SSID dev wifi | grep '^yes' | cut -d':' -f2"
            current_ssid = subprocess.check_output(cmd, shell=True, encoding='utf-8').strip()
            is_home = current_ssid.startswith(WIFI_PREFIX)

            if is_home != garage_mode_active:
                garage_mode_active = is_home
        except Exception:
            pass

        time.sleep(60)

threading.Thread(target=check_wifi_geofence, daemon=True).start()

def record_loop():
    """Manages FFmpeg with direct stream copy for low CPU and small file sizes."""
    # Clean up any leftover stream files in RAM on startup
    for f in os.listdir(RAM_DISK):
        if f.startswith('stream') or f.endswith('.tmp_srt'):
            try:
                os.remove(os.path.join(RAM_DISK, f))
            except Exception:
                pass

    while True:
        if garage_mode_active:
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-f", "v4l2", "-input_format", "h264", "-video_size", "1920x1080", "-framerate", "30",
                "-i", "/dev/video0", "-c:v", "copy", "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments", os.path.join(RAM_DISK, "stream.m3u8")
            ]
        else:
            now = datetime.datetime.now()
            date_folder = now.strftime("%Y-%m-%d")
            ts = now.strftime("%Y%m%d_%H%M%S")

            # Create a daily folder structure (e.g., /mnt/dashcam/2026-02-26/)
            daily_path = os.path.join(DISK_PATH, date_folder)
            os.makedirs(daily_path, exist_ok=True)

            mp4_file = os.path.join(daily_path, f"SilverADO_{ts}.mp4")
            final_srt = os.path.join(daily_path, f"SilverADO_{ts}.srt")

            # Optimized FFmpeg command.
            # Removed the software video filter (-vf) and libx264.
            # Replaced with direct stream copy (-c:v copy).
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-f", "v4l2", "-input_format", "h264", "-video_size", "1920x1080", "-framerate", "30",
                "-i", "/dev/video0",
                "-t", str(CHUNK_SECONDS),
                "-c:v", "copy",
                "-movflags", "+frag_keyframe+empty_moov+omit_tfhd_offset+default_base_moof",
                "-flush_packets", "1",
                mp4_file,
                "-c:v", "copy", "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments", os.path.join(RAM_DISK, "stream.m3u8")
            ]

            # Write subtitles directly to the permanent disk, not RAM
            threading.Thread(target=generate_srt, args=(final_srt,), daemon=True).start()

        process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        while process.poll() is None:
            # If the mode changes (e.g., pulling into the garage), terminate the current recording chunk
            if garage_mode_active == ("mp4" in str(ffmpeg_cmd)):
                process.terminate()
                break
            time.sleep(10)

        process.wait()

        # The Hardware Release Buffer
        # Give the Linux kernel time to release the /dev/video0 interface before looping.
        # This prevents the "Device Busy" crash that kills recordings after the first chunk.
        time.sleep(HW_BUFFER_SECONDS)

def generate_srt(srt_file):
    """Generates a synchronous subtitle file with live telemetry data."""
    srt_index = 1
    start_time = time.time()
    psutil.cpu_percent(interval=None)

    try:
        with open(srt_file, "w") as f:
            while srt_index <= CHUNK_SECONDS and not garage_mode_active:
                elapsed = time.time() - start_time
                cpu = psutil.cpu_percent(interval=None)

                start_ts = str(datetime.timedelta(seconds=int(elapsed))) + ",000"
                end_ts = str(datetime.timedelta(seconds=int(elapsed + 1))) + ",000"

                f.write(f"{srt_index}\n{start_ts} --> {end_ts}\n")
                f.write(f"SILVERADO | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | CPU: {cpu}% | MODE: ROAD\n\n")

                f.flush()
                os.fsync(f.fileno())  # Force OS to physically write to disk every second

                srt_index += 1
                time.sleep(1.0)
    except Exception:
        pass

@app.route('/stream.m3u8')
def stream_m3u8():
    return send_from_directory(RAM_DISK, 'stream.m3u8')

@app.route('/<path:filename>')
def stream_ts(filename):
    if filename.endswith('.ts'):
        return send_from_directory(RAM_DISK, filename)
    return "Not found", 404

if __name__ == "__main__":
    threading.Thread(target=record_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, threaded=True)

