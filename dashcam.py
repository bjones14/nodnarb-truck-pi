import argparse
import datetime
import json
import os
import shutil
import signal
import subprocess
import threading
import time

import psutil
from flask import Flask, send_from_directory

app = Flask(__name__)

# --- PARSE COMMAND LINE ARGS ---
parser = argparse.ArgumentParser(description="Silverado Dashcam Service")
parser.add_argument('--ignore-parked', action='store_true', help="Force continuous MKV recording to disk.")
parser.add_argument('--chunk-seconds', type=int, default=900, help="Duration of video chunk in seconds.")
parser.add_argument('--hw-buffer', type=int, default=2, help="Hardware release buffer time in seconds.")
parser.add_argument('--compress', action='store_true', help="Use hardware encoder to shrink file size.")
parser.add_argument('--bitrate', type=str, default="6M", help="Target bitrate for compression.")
parser.add_argument('--flip', action='store_true', help="Flip the video 180 degrees.")
parser.add_argument('--mic', type=str, default="plughw:CARD=Stream,DEV=0", help="ALSA audio device.")
args, unknown = parser.parse_known_args()

IGNORE_PARKED = args.ignore_parked
CHUNK_SECONDS = args.chunk_seconds
HW_BUFFER_SECONDS = args.hw_buffer
USE_COMPRESSION = args.compress
TARGET_BITRATE = args.bitrate
FLIP_VIDEO = args.flip
AUDIO_MIC = args.mic

# --- CONFIGURATION ---
RAM_DISK = "/dev/shm/dashcam_stream"
DISK_PATH = "/mnt/dashcam"
MAX_DISK_USAGE_PERCENT = 90


def get_telemetry_raw():
    try:
        if os.path.exists("/dev/shm/telemetry.json"):
            with open("/dev/shm/telemetry.json", "r") as f:
                return json.load(f)
    except Exception as e:
        print(f"Warning: Failed to read telemetry JSON: {e}")
    return {}


def is_driving():
    if IGNORE_PARKED:
        return True
    return get_telemetry_raw().get('truck_awake', False)


def is_camera_present():
    return os.path.exists("/dev/video0")


def init_camera_focus():
    """Forces the Logitech C922 to lock focus to infinity, disabling autofocus hunting."""
    try:
        subprocess.run(["v4l2-ctl", "-d", "/dev/video0", "-c", "focus_auto=0"],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["v4l2-ctl", "-d", "/dev/video0", "-c", "focus_absolute=0"],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["v4l2-ctl", "-d", "/dev/video0", "-c", "power_line_frequency=1"],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"Warning: Could not set hardware camera controls: {e}")


def ensure_paths():
    """Ensures directories exist and cleans old RAM stream files."""
    try:
        os.makedirs(RAM_DISK, exist_ok=True)
    except Exception as e:
        print(f"Error creating RAM disk directory: {e}")

    if os.path.exists(RAM_DISK):
        for f in os.listdir(RAM_DISK):
            if f.startswith('stream'):
                try:
                    os.remove(os.path.join(RAM_DISK, f))
                except Exception:
                    pass


def cleanup_old_footage():
    try:
        if not os.path.exists(DISK_PATH):
            return
        usage = psutil.disk_usage(DISK_PATH)
        if usage.percent > MAX_DISK_USAGE_PERCENT:
            folders = sorted([f for f in os.listdir(DISK_PATH)
                              if os.path.isdir(os.path.join(DISK_PATH, f))])
            if folders:
                print(f"Disk full ({usage.percent}%). Deleting oldest folder: {folders[0]}")
                shutil.rmtree(os.path.join(DISK_PATH, folders[0]))
    except Exception as e:
        print(f"Error during footage cleanup: {e}")


def get_telemetry():
    """Safely extracts and formats telemetry data, guarding against missing or null JSON values."""
    d = get_telemetry_raw()

    def safe_float(key):
        try:
            val = d.get(key)
            return float(val) if val is not None else 0.0
        except (ValueError, TypeError):
            return 0.0

    v = f"{safe_float('battery_voltage'):.1f}"
    a = f"{safe_float('current_amps'):.1f}"
    soc = f"{safe_float('soc_percent'):.1f}"

    return v, a, soc


def generate_srt(srt_file, mkv_file, stop_event):
    """Generates subtitle file, but ONLY if the MKV video file is successfully created first."""
    file_ready = False

    # Wait up to 10 seconds for FFmpeg to actually create and start writing to the MKV
    for _ in range(10):
        if stop_event.is_set():
            return
        if os.path.exists(mkv_file) and os.path.getsize(mkv_file) > 0:
            file_ready = True
            break
        time.sleep(1.0)

    if not file_ready:
        print(f"Notice: Video {mkv_file} failed to start. Skipping SRT creation.")
        return

    idx = 1
    start = time.monotonic()
    try:
        with open(srt_file, "w") as f:
            while idx <= CHUNK_SECONDS and not stop_event.is_set():
                elapsed = time.monotonic() - start
                ts_now = datetime.datetime.now().strftime('%I:%M:%S %p')
                v, a, soc = get_telemetry()

                f.write(f"{idx}\n{str(datetime.timedelta(seconds=int(elapsed)))},000 --> "
                        f"{str(datetime.timedelta(seconds=int(elapsed+1)))},000\n")

                # Updated subtitle format: Time | Bat | SoC
                f.write(f"{ts_now} | Bat: {v}V {a}A | SoC: {soc}%\n\n")
                f.flush()

                idx += 1
                time.sleep(1.0)
    except Exception as e:
        print(f"Error writing to SRT file: {e}")


def record_loop():
    while True:
        if not is_camera_present():
            time.sleep(5.0)
            continue

        if not is_driving():
            time.sleep(2.0)
            continue

        # --- TRUCK IS AWAKE: START RECORDING ---
        ensure_paths()
        cleanup_old_footage()
        init_camera_focus()

        now = datetime.datetime.now()
        ts = now.strftime("%Y%m%d_%H%M%S")
        daily_path = os.path.join(DISK_PATH, now.strftime("%Y-%m-%d"))

        try:
            os.makedirs(daily_path, exist_ok=True)
        except Exception as e:
            print(f"Cannot write to SD card: {e}")
            time.sleep(5.0)
            continue

        mkv_file = os.path.join(daily_path, f"Silverado_{ts}.mkv")
        final_srt = os.path.join(daily_path, f"Silverado_{ts}.srt")
        hls_playlist = os.path.join(RAM_DISK, 'stream.m3u8')
        hls_segment_template = os.path.join(RAM_DISK, 'stream%d.ts')

        stop_srt_event = threading.Event()

        # Pass the mkv_file into the thread so it can monitor it
        threading.Thread(target=generate_srt, args=(final_srt, mkv_file, stop_srt_event), daemon=True).start()

        encode_bitrate = TARGET_BITRATE if USE_COMPRESSION else "8M"

        # Base Video Input (MJPEG from webcam)
        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-f", "v4l2", "-input_format", "mjpeg", "-video_size", "1920x1080", "-framerate", "30",
            "-t", str(CHUNK_SECONDS), "-i", "/dev/video0"
        ]

        # Add Audio Input (ALSA)
        if AUDIO_MIC:
            ffmpeg_cmd += ["-f", "alsa", "-channels", "2", "-i", AUDIO_MIC]

        # Filter construction: Includes the audio high-pass and aggressive limiter
        v_filter = "vflip,hflip,format=yuv420p" if FLIP_VIDEO else "format=yuv420p"
        a_filter = "highpass=f=100,alimiter=limit=0.4:level=0:attack=2:release=50"

        # Encoders (Hardware H264 + AAC)
        v_codec = ["-c:v", "h264_v4l2m2m", "-b:v", encode_bitrate, "-num_capture_buffers", "32", "-vf", v_filter]
        a_codec = ["-c:a", "aac", "-b:a", "128k", "-af", a_filter] if AUDIO_MIC else []

        ffmpeg_cmd += v_codec + a_codec

        # Stream Mapping and Tee configuration
        if AUDIO_MIC:
            ffmpeg_cmd += ["-map", "0:v", "-map", "1:a"]
        else:
            ffmpeg_cmd += ["-map", "0:v"]

        tee_map = (
            f"[f=matroska]{mkv_file}|"
            f"[f=hls:hls_time=2:hls_list_size=5:hls_flags=delete_segments:"
            f"hls_segment_filename={hls_segment_template}]{hls_playlist}"
        )

        ffmpeg_cmd += ["-flags", "+global_header", "-f", "tee", tee_map]

        try:
            process = subprocess.Popen(ffmpeg_cmd)
            while process.poll() is None:
                if not is_camera_present() or not is_driving():
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    break
                time.sleep(1)
            process.wait()
        except Exception as e:
            print(f"FFmpeg process encountered an error: {e}")

        stop_srt_event.set()
        time.sleep(HW_BUFFER_SECONDS)


@app.route('/stream.m3u8')
def stream_m3u8():
    return send_from_directory(RAM_DISK, 'stream.m3u8')


@app.route('/<path:filename>')
def stream_ts(filename):
    if filename.endswith('.ts'):
        return send_from_directory(RAM_DISK, filename)
    return "Not found", 404


if __name__ == "__main__":
    print("Starting Silverado Dashcam Service...")
    threading.Thread(target=record_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)

