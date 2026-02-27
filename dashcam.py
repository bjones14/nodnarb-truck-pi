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
parser.add_argument('--chunk-seconds', type=int, default=2700, help="Duration of video chunk in seconds.")
parser.add_argument('--hw-buffer', type=int, default=2, help="Hardware release buffer time in seconds.")
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
    config = {
        "paths": {"dashcam_mount": "/mnt/dashcam", "ram_disk": "/mnt/ramdisk"},
        "wifi": {"prefix": "Silverado_Guest"}
    }

# --- CONFIGURATION FROM FILE ---
DISK_PATH = config['paths']['dashcam_mount']
RAM_DISK = config['paths']['ram_disk']
WIFI_PREFIX = config['wifi'].get('prefix', 'Silverado_Guest')

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
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [MODE] Switching to {'GARAGE' if is_home else 'ROAD'} Mode")
                garage_mode_active = is_home
        except Exception:
            pass
        time.sleep(60)

threading.Thread(target=check_wifi_geofence, daemon=True).start()

def is_camera_present():
    """Checks if the camera hardware is physically connected."""
    return os.path.exists("/dev/video0")

def cleanup_ram_disk():
    """Cleans up the RAM disk and loudly alerts if there are permission issues (root vs user)."""
    if os.path.exists(RAM_DISK):
        for f in os.listdir(RAM_DISK):
            if f.startswith('stream') or f.endswith('.tmp_srt'):
                file_path = os.path.join(RAM_DISK, f)
                try:
                    os.remove(file_path)
                except PermissionError:
                    print(f"[CRITICAL] Cannot delete {file_path}!")
                    print(f"           Permission Denied. Was it created by root/sudo?")
                    print(f"           Run this manually: sudo rm -f {file_path}")
                except Exception:
                    pass

def cleanup_old_footage():
    """Checks disk usage and deletes the oldest date folder if over the limit."""
    try:
        usage = psutil.disk_usage(DISK_PATH)
        if usage.percent > MAX_DISK_USAGE_PERCENT:
            all_entries = os.listdir(DISK_PATH)
            folders = sorted([f for f in all_entries if os.path.isdir(os.path.join(DISK_PATH, f))])
            if folders:
                oldest_folder = os.path.join(DISK_PATH, folders[0])
                print(f"[STORAGE] Disk at {usage.percent}%. Purging: {oldest_folder}")
                shutil.rmtree(oldest_folder)
    except Exception: pass

def record_loop():
    """Manages FFmpeg with massive buffering for thumb drive latency."""
    print(f"--- DASHCAM STARTUP ---")
    print(f"Target Duration: {CHUNK_SECONDS}s")
    print(f"Ignore Parked: {IGNORE_PARKED}")
    print(f"-----------------------")

    cleanup_ram_disk()

    while True:
        # Wait for camera hardware
        if not is_camera_present():
            print("[HARDWARE] Camera /dev/video0 NOT FOUND. Checking again in 5s...")
            while not is_camera_present():
                time.sleep(5.0)
            print("[HARDWARE] Camera reconnected!")

        if garage_mode_active:
            # GARAGE MODE: HLS STREAM ONLY (Infinite)
            ffmpeg_cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-use_wallclock_as_timestamps", "1", "-fflags", "+genpts",
                "-f", "v4l2", "-input_format", "h264", "-video_size", "1920x1080", "-framerate", "30",
                "-thread_queue_size", "4096",
                "-i", "/dev/video0",
                "-c:v", "copy",
                "-max_muxing_queue_size", "9999",
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments", os.path.join(RAM_DISK, "stream.m3u8")
            ]
        else:
            # ROAD MODE: MP4 + HLS (Limited by CHUNK_SECONDS)
            cleanup_old_footage()
            now = datetime.datetime.now()
            daily_path = os.path.join(DISK_PATH, now.strftime("%Y-%m-%d"))
            os.makedirs(daily_path, exist_ok=True)

            ts = now.strftime("%Y%m%d_%H%M%S")
            mp4_file = os.path.join(daily_path, f"Silverado_{ts}.mp4")
            final_srt = os.path.join(daily_path, f"Silverado_{ts}.srt")

            print(f"[{now.strftime('%H:%M:%S')}] [RECORDING] Start: Silverado_{ts}.mp4 ({CHUNK_SECONDS}s)")

            ffmpeg_cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-use_wallclock_as_timestamps", "1", "-fflags", "+genpts",
                "-f", "v4l2", "-input_format", "h264", "-video_size", "1920x1080", "-framerate", "30",
                "-t", str(CHUNK_SECONDS),
                "-thread_queue_size", "4096",
                "-i", "/dev/video0",
                "-c:v", "copy",
                "-max_muxing_queue_size", "9999",
                "-movflags", "+frag_keyframe+empty_moov+omit_tfhd_offset+default_base_moof",
                mp4_file,
                "-c:v", "copy",
                "-max_muxing_queue_size", "9999",
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments", os.path.join(RAM_DISK, "stream.m3u8")
            ]
            threading.Thread(target=generate_srt, args=(final_srt,), daemon=True).start()

        try:
            process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL)

            while process.poll() is None:
                if garage_mode_active == ("mp4" in str(ffmpeg_cmd)):
                    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [SYSTEM] Mode changed, terminating process.")
                    process.terminate()
                    break

                if not is_camera_present():
                    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [CRITICAL] Camera disconnected.")
                    process.terminate()
                    break

                time.sleep(2)

            process.wait()

        except Exception as e:
            print(f"[CRITICAL] Loop error: {e}")

        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [SYSTEM] Chunk finished.")
        time.sleep(HW_BUFFER_SECONDS)

def get_telemetry():
    """Reads live vehicle sensors shared by telemetry.py via RAM disk."""
    try:
        with open("/dev/shm/telemetry.json", "r") as f:
            data = json.load(f)

        # Temperature (Convert C to F if valid, otherwise fallback to N/A)
        cabin_c = data.get("cabin_temp_c")
        if cabin_c is not None:
            temp_str = f"{(cabin_c * 9/5) + 32:.0f}F"
        else:
            temp_str = "N/A"

        # Voltage & Current (Formatted to 1 decimal place)
        volts_str = f"{data.get('battery_voltage', 0.0):.1f}"
        amps_str = f"{data.get('current_amps', 0.0):.1f}"

        return temp_str, volts_str, amps_str
    except Exception:
        # If telemetry.py isn't running or file is missing, return placeholders
        return "N/A", "--.-", "--.-"

def generate_srt(srt_file):
    """Generates a synchronous subtitle file with live telemetry data."""
    srt_index = 1
    start_time = time.monotonic()
    try:
        with open(srt_file, "w") as f:
            current_mode = garage_mode_active
            while srt_index <= CHUNK_SECONDS and garage_mode_active == current_mode:
                if not is_camera_present(): break
                elapsed = time.monotonic() - start_time
                cpu = psutil.cpu_percent(interval=None)

                start_ts = str(datetime.timedelta(seconds=int(elapsed))) + ",000"
                end_ts = str(datetime.timedelta(seconds=int(elapsed + 1))) + ",000"

                # Format: YY-MM-DD HH:MM:SS AM/PM
                timestamp_str = datetime.datetime.now().strftime('%y-%m-%d %I:%M:%S %p')
                temp, volts, amps = get_telemetry()

                f.write(f"{srt_index}\n{start_ts} --> {end_ts}\n")
                f.write(f"{timestamp_str} | CPU: {cpu}% | Temp: {temp} | Bat: {volts}V {amps}A\n\n")

                f.flush()
                os.fsync(f.fileno())
                srt_index += 1
                time.sleep(1.0)
    except Exception: pass

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

