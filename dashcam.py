import os
import time
import threading
import datetime
import subprocess
import psutil
import json
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

# --- IMAGE QUALITY TUNING ---
# These flags help with overexposure at night.
# We reduce brightness and increase contrast to keep light sources from 'blooming'.
VIDEO_PARAMS = [
    "-vf", "eq=brightness=-0.1:contrast=1.2:saturation=1.1",
]

# Global State
garage_mode_active = False

def check_wifi_geofence():
    """Polls for home networks every 60s."""
    global garage_mode_active
    while True:
        try:
            cmd = "nmcli -t -f ACTIVE,SSID dev wifi | grep '^yes' | cut -d':' -f2"
            current_ssid = subprocess.check_output(cmd, shell=True, encoding='utf-8').strip()
            is_home = current_ssid.startswith(WIFI_PREFIX)
            if is_home != garage_mode_active:
                garage_mode_active = is_home
        except:
            pass
        time.sleep(60)

threading.Thread(target=check_wifi_geofence, daemon=True).start()

def record_loop():
    """Manages FFmpeg with corruption-resistant flags and image tuning."""
    for f in os.listdir(RAM_DISK):
        if f.startswith('stream') or f.endswith('.tmp_srt'):
            try: os.remove(os.path.join(RAM_DISK, f))
            except: pass

    while True:
        if garage_mode_active:
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-f", "v4l2", "-input_format", "h264", "-video_size", "1920x1080", "-framerate", "30",
                "-i", "/dev/video0", "-c:v", "copy", "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments", os.path.join(RAM_DISK, "stream.m3u8")
            ]
        else:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            mp4_file = os.path.join(DISK_PATH, f"SilverADO_{ts}.mp4")
            tmp_srt = os.path.join(RAM_DISK, f"SilverADO_{ts}.tmp_srt")
            final_srt = os.path.join(DISK_PATH, f"SilverADO_{ts}.srt")

            ffmpeg_cmd = [
                "ffmpeg", "-y", "-f", "v4l2", "-input_format", "h264", "-video_size", "1920x1080", "-framerate", "30",
                "-i", "/dev/video0",
                "-t", str(CHUNK_SECONDS),
                # Apply Image Correction filters
                "-vf", "eq=brightness=-0.05:contrast=1.3",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "25",
                "-movflags", "+frag_keyframe+empty_moov+omit_tfhd_offset+default_base_moof",
                mp4_file,
                "-c:v", "copy", "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                "-hls_flags", "delete_segments", os.path.join(RAM_DISK, "stream.m3u8")
            ]
            threading.Thread(target=generate_srt, args=(tmp_srt,), daemon=True).start()

        process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        while process.poll() is None:
            if garage_mode_active == ("mp4" in str(ffmpeg_cmd)):
                process.terminate()
                break
            time.sleep(10)

        process.wait()

        if not garage_mode_active and os.path.exists(tmp_srt):
            try:
                with open(tmp_srt, 'r') as fr, open(final_srt, 'w') as fw:
                    fw.write(fr.read())
                os.remove(tmp_srt)
            except: pass

def generate_srt(srt_file):
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
                srt_index += 1
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
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, threaded=True)
