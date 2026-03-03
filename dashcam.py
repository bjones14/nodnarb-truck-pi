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
parser.add_argument('--ignore-parked', action='store_true', help="Force continuous MP4 recording to disk, ignoring telemetry.")
parser.add_argument('--chunk-seconds', type=int, default=900, help="Duration of video chunk in seconds (Default 15m).")
parser.add_argument('--hw-buffer', type=int, default=2, help="Hardware release buffer time in seconds.")
parser.add_argument('--compress', action='store_true', help="Use Pi 4 hardware encoder to shrink file size.")
parser.add_argument('--bitrate', type=str, default="3.5M", help="Target bitrate for compression (e.g., 2M, 4M, 500K).")
parser.add_argument('--flip', action='store_true', help="Flip the video 180 degrees for upside-down mounting.")
args, unknown = parser.parse_known_args()

IGNORE_PARKED = args.ignore_parked
CHUNK_SECONDS = args.chunk_seconds
HW_BUFFER_SECONDS = args.hw_buffer
USE_COMPRESSION = args.compress
TARGET_BITRATE = args.bitrate
FLIP_VIDEO = args.flip

# --- CONFIGURATION ---
RAM_DISK = "/dev/shm/dashcam_stream"
DISK_PATH = "/mnt/dashcam"
MAX_DISK_USAGE_PERCENT = 90

def get_telemetry_raw():
    try:
        with open("/dev/shm/telemetry.json", "r") as f:
            return json.load(f)
    except Exception:
        return {}

def is_driving():
    if IGNORE_PARKED:
        return True
    return get_telemetry_raw().get('truck_awake', False)

def is_camera_present():
    return os.path.exists("/dev/video0")

def ensure_paths():
    """Ensures directories exist and are clean."""
    if not os.path.exists(RAM_DISK):
        os.makedirs(RAM_DISK, exist_ok=True)
    else:
        # Clean up old stream files
        for f in os.listdir(RAM_DISK):
            if f.startswith('stream'):
                try: os.remove(os.path.join(RAM_DISK, f))
                except: pass

def cleanup_old_footage():
    try:
        if not os.path.exists(DISK_PATH): return
        usage = psutil.disk_usage(DISK_PATH)
        if usage.percent > MAX_DISK_USAGE_PERCENT:
            folders = sorted([f for f in os.listdir(DISK_PATH) if os.path.isdir(os.path.join(DISK_PATH, f))])
            if folders:
                shutil.rmtree(os.path.join(DISK_PATH, folders[0]))
    except Exception: pass

def record_loop():
    while True:
        ensure_paths()
        if not is_camera_present():
            time.sleep(5.0)
            continue

        currently_driving = is_driving()
        cleanup_old_footage()

        now = datetime.datetime.now()
        ts = now.strftime("%Y%m%d_%H%M%S")

        hls_playlist = os.path.join(RAM_DISK, 'stream.m3u8')
        hls_out = f"[f=hls:hls_time=2:hls_list_size=5:hls_flags=delete_segments]{hls_playlist}"

        if currently_driving:
            daily_path = os.path.join(DISK_PATH, now.strftime("%Y-%m-%d"))
            os.makedirs(daily_path, exist_ok=True) # CRITICAL FIX: Ensure the daily folder exists
            mp4_file = os.path.join(daily_path, f"Silverado_{ts}.mp4")
            final_srt = os.path.join(daily_path, f"Silverado_{ts}.srt")

            mp4_out = f"[f=mp4:movflags=+frag_keyframe+empty_moov]{mp4_file}"
            tee_map = f"{mp4_out}|{hls_out}"

            stop_srt_event = threading.Event()
            threading.Thread(target=generate_srt, args=(final_srt, stop_srt_event), daemon=True).start()
        else:
            tee_map = hls_out
            stop_srt_event = None

        encode_bitrate = TARGET_BITRATE if USE_COMPRESSION else "8M"

        # Build filter string
        # format=yuv420p is required for the hardware encoder to produce valid video (not black)
        filters = "format=yuv420p"
        if FLIP_VIDEO:
            filters = f"vflip,hflip,{filters}"

        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "v4l2", "-input_format", "mjpeg", "-video_size", "1920x1080", "-framerate", "30",
            "-t", str(CHUNK_SECONDS), "-i", "/dev/video0",
            "-vf", filters,
            "-c:v", "h264_v4l2m2m", "-b:v", encode_bitrate, "-num_capture_buffers", "32",
            "-f", "tee", "-map", "0:v", tee_map
        ]

        try:
            process = subprocess.Popen(ffmpeg_cmd)
            while process.poll() is None:
                if not is_camera_present():
                    process.terminate()
                    break
                if is_driving() != currently_driving:
                    process.terminate()
                    break
                time.sleep(1)
            process.wait()
        except Exception: pass

        if stop_srt_event:
            stop_srt_event.set()

        time.sleep(HW_BUFFER_SECONDS)

def get_telemetry():
    d = get_telemetry_raw()
    temp = f"{(d.get('cabin_temp_c', 0)*9/5)+32:.0f}F" if d.get("cabin_temp_c") else "N/A"
    return temp, f"{d.get('battery_voltage',0):.1f}", f"{d.get('current_amps',0):.1f}"

def generate_srt(srt_file, stop_event):
    idx = 1
    start = time.monotonic()
    try:
        with open(srt_file, "w") as f:
            while idx <= CHUNK_SECONDS and not stop_event.is_set():
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
def stream_m3u8():
    return send_from_directory(RAM_DISK, 'stream.m3u8')

@app.route('/<path:filename>')
def stream_ts(filename):
    if filename.endswith('.ts'):
        return send_from_directory(RAM_DISK, filename)
    return "Not found", 404

if __name__ == "__main__":
    threading.Thread(target=record_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)
