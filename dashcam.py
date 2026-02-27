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
parser.add_argument('--chunk-seconds', type=int, default=900, help="Duration of each video chunk in seconds (default: 900).")
parser.add_argument('--hw-buffer', type=int, default=2, help="Hardware release buffer time in seconds (default: 2).")
args, unknown = parser.parse_known_args()

IGNORE_PARKED = args.ignore_parked
CHUNK_SECONDS = args.chunk_seconds
HW_BUFFER_SECONDS = args.hw_buffer

# Load Configuration
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
try:
    with open(CONFIG_PATH, 'r') as f:
        config = json.load(f)
except Exception:
    # Fallback defaults if config is missing during testing
    config = {
        "paths": {"dashcam_mount": "/mnt/dashcam", "ram_disk": "/mnt/ramdisk"},
        "wifi": {"prefix": "Silverado_Guest"}
    }

# --- CONFIGURATION FROM FILE ---
DISK_PATH = config['paths']['dashcam_mount']
RAM_DISK = config['paths']['ram_disk']
WIFI_PREFIX = config['wifi']['prefix']

MAX_DISK_USAGE_PERCENT = 90

# Global State
garage_mode_active = False

def check_wifi_geofence():
    """Polls for home networks every 60s."""
    global garage_mode_active

    if IGNORE_PARKED:
        print("[STATUS] Flag --ignore-parked active. Locked in ROAD MODE.")
        garage_mode_active = False
        return

    while True:
        try:
            cmd = "nmcli -t -f ACTIVE,SSID dev wifi | grep '^yes' | cut -d':' -f2"
            current_ssid = subprocess.check_output(cmd, shell=True, encoding='utf-8').strip()
            is_home = current_ssid.startswith(WIFI_PREFIX)

            if is_home != garage_mode_active:
                print(f"[STATUS] Network Change: {'Garage' if is_home else 'Road'} Mode")
                garage_mode_active = is_home
        except Exception:
            pass

        time.sleep(60)

threading.Thread(target=check_wifi_geofence, daemon=True).start()

def is_camera_ready():
    """Verifies that the camera device exists and is available."""
    return os.path.exists("/dev/video0")

def cleanup_old_footage():
    """Checks disk usage and deletes the oldest date folder if over the limit."""
    try:
        # Check usage of the partition where DISK_PATH is mounted
        usage = psutil.disk_usage(DISK_PATH)
        if usage.percent > MAX_DISK_USAGE_PERCENT:
            print(f"[STORAGE] Disk usage at {usage.percent}%. Threshold is {MAX_DISK_USAGE_PERCENT}%.")

            # Get list of folders (named YYYY-MM-DD)
            # Sorting alphabetically works perfectly for date-based folder names
            all_entries = os.listdir(DISK_PATH)
            folders = sorted([f for f in all_entries if os.path.isdir(os.path.join(DISK_PATH, f))])

            if folders:
                oldest_folder = os.path.join(DISK_PATH, folders[0])
                print(f"[STORAGE] Purging oldest footage folder: {oldest_folder}")
                shutil.rmtree(oldest_folder)
            else:
                print("[STORAGE] Warning: Disk is full but no daily folders were found to delete.")
    except Exception as e:
        print(f"[STORAGE] Error during disk cleanup: {e}")

def record_loop():
    """Manages FFmpeg with improved hardware availability and pipe management."""
    # Clean up RAM disk on startup
    if os.path.exists(RAM_DISK):
        for f in os.listdir(RAM_DISK):
            if f.startswith('stream') or f.endswith('.tmp_srt'):
                try:
                    os.remove(os.path.join(RAM_DISK, f))
                except Exception:
                    pass

    while True:
        # Wait for the hardware to be present before starting
        while not is_camera_ready():
            print("[HARDWARE] Camera missing. Retrying in 5s...")
            time.sleep(5.0)

        if garage_mode_active:
            ffmpeg_cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-f", "v4l2", "-input_format", "h264", "-video_size", "1920x1080", "-framerate", "30",
                "-i", "/dev/video0", "-c:v", "copy", "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments", os.path.join(RAM_DISK, "stream.m3u8")
            ]
        else:
            # Check and clean disk space before starting a new Road Mode chunk
            cleanup_old_footage()

            now = datetime.datetime.now()
            date_folder = now.strftime("%Y-%m-%d")
            ts = now.strftime("%Y%m%d_%H%M%S")

            daily_path = os.path.join(DISK_PATH, date_folder)
            os.makedirs(daily_path, exist_ok=True)

            mp4_file = os.path.join(daily_path, f"Silverado_{ts}.mp4")
            final_srt = os.path.join(daily_path, f"Silverado_{ts}.srt")

            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [RECORDING] Starting chunk: Silverado_{ts}.mp4")

            # Applying -t to the INPUT ensures all outputs (MP4 and HLS) stop at the same time
            ffmpeg_cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-f", "v4l2", "-input_format", "h264", "-video_size", "1920x1080", "-framerate", "30",
                "-t", str(CHUNK_SECONDS),
                "-i", "/dev/video0",
                "-c:v", "copy",
                "-movflags", "+frag_keyframe+empty_moov+omit_tfhd_offset+default_base_moof",
                "-flush_packets", "1",
                mp4_file,
                "-c:v", "copy", "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments", os.path.join(RAM_DISK, "stream.m3u8")
            ]

            threading.Thread(target=generate_srt, args=(final_srt,), daemon=True).start()

        try:
            process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            while process.poll() is None:
                if garage_mode_active == ("mp4" in str(ffmpeg_cmd)):
                    process.terminate()
                    break
                time.sleep(2)

            process.wait()

        except Exception as e:
            print(f"[CRITICAL] Loop error: {e}")

        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [SYSTEM] Chunk finished. Pausing {HW_BUFFER_SECONDS}s for HW release...")
        time.sleep(HW_BUFFER_SECONDS)

def generate_srt(srt_file):
    """Generates a synchronous subtitle file with live telemetry data."""
    srt_index = 1
    start_time = time.monotonic()
    psutil.cpu_percent(interval=None)

    try:
        with open(srt_file, "w") as f:
            while srt_index <= CHUNK_SECONDS and not garage_mode_active:
                elapsed = time.monotonic() - start_time
                cpu = psutil.cpu_percent(interval=None)

                start_ts = str(datetime.timedelta(seconds=int(elapsed))) + ",000"
                end_ts = str(datetime.timedelta(seconds=int(elapsed + 1))) + ",000"

                f.write(f"{srt_index}\n{start_ts} --> {end_ts}\n")
                f.write(f"Silverado | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | CPU: {cpu}% | MODE: ROAD\n\n")

                f.flush()
                os.fsync(f.fileno())

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

