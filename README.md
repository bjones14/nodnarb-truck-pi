# **Silverado Pi Telemetry & Dashcam System**

A robust, DIY automotive telemetry node and smart dashcam built on a Raspberry Pi. Designed specifically for a Chevy Silverado, this project monitors battery health, tracks heavy current draws (like starter motor cranking), controls a smart dashcam based on WiFi geofencing, and integrates seamlessly into Home Assistant via MQTT.

## **🚀 Features**

* **Live Electrical Telemetry:** Monitors Main Battery Voltage, Ignition/RAP Voltage, and net Current Draw (Amps) using a differential Hall effect sensor and an ADS1115 ADC.  
* **Smart Dashcam:** Automatically switches between a live HLS stream (when parked at home) and continuous 15-minute MP4 recording chunks (when driving).  
* **Live Video Subtitles:** Generates synchronous .srt files embedding live Pi CPU metrics and timestamps directly into the dashcam footage.  
* **Home Assistant Auto-Discovery:** Instantly populates sensors in Home Assistant without manual YAML configuration.  
* **Battery Protection:** Automatically halts the Raspberry Pi if the truck's battery drops below a critical threshold (11.2V) to prevent dead batteries during long-term parking.  
* **Thermal Management:** Reads cabin temperature via a 1-Wire DS18B20 sensor and controls an Argon case fan via I2C based on CPU temperature curves.

## **🛠 Hardware Requirements**

* Raspberry Pi (Tested on Pi 4\)  
* ADS1115 16-Bit ADC (I2C)  
* Bi-directional Hall Effect Current Sensor (e.g., \+/- 200A)  
* DS18B20 1-Wire Temperature Sensor  
* USB Web Camera (compatible with /dev/video0)  
* 12V to 5V Step-Down Converter  
* Standard Resistors for Voltage Dividers (10kΩ / 2.2kΩ)

## **📦 Software Dependencies**

### **System Packages**

The scripts rely on a few core Linux utilities. Ensure they are installed on your Pi:

sudo apt update  
sudo apt install ffmpeg network-manager

### **Python Libraries**

Install the required Python packages using pip:

pip3 install paho-mqtt psutil adafruit-blinka adafruit-circuitpython-ads1x15 flask

*(Note: adafruit-blinka provides the board and busio libraries required for I2C communication).*

## **⚙️ Configuration**

Before running the scripts, create or modify the config.json file in the root directory. This file dictates where the dashcam saves footage and how the Pi connects to Home Assistant.

{  
  "paths": {  
    "dashcam\_mount": "/mnt/dashcam",  
    "ram\_disk": "/mnt/ramdisk"  
  },  
  "wifi": {  
    "prefix": "YourHomeWiFiNetwork"  
  },  
  "mqtt": {  
    "broker": "192.168.1.100",  
    "user": "your\_mqtt\_user",  
    "pass": "your\_mqtt\_password"  
  }  
}

* **dashcam\_mount**: The permanent storage directory (SSD/SD Card) for MP4 road recordings.  
* **ram\_disk**: A tmpfs RAM disk path used to save wear and tear on the SD card while streaming in Garage Mode.  
* **prefix**: The first few characters of your Home WiFi SSID. Used for the Geofence.

## **📜 The Scripts**

### **1\. telemetry.py**

This is the core background daemon. It handles all hardware polling, mathematical conversions, and state logic.

**What it does:**

1. Polls the ADS1115 for voltages and calculates net amperage.  
2. Uses a fast-polling loop (10 times a second) when the truck is awake to catch split-second transient loads, like a 600A starter motor crank.  
3. Automatically starts and stops the dashcam.service based on the Ignition Voltage.  
4. Pushes all data to your MQTT broker.

**Calibration:**

If you need to calibrate your sensors, edit the constants at the top of telemetry.py:

* DIVIDER\_FACTOR: Adjust based on your physical resistors.  
* CURRENT\_ZERO\_OFFSET\_V: The "Tare Weight" of your specific Hall sensor at 0 Amps.  
* CURRENT\_SENSITIVITY\_V\_PER\_A: The mV/A rating of your Hall sensor.

**Usage:**

python3 telemetry.py

### **2\. dashcam.py**

A smart, adaptive dashcam service utilizing ffmpeg.

**What it does:**

* **Parked/Garage Mode:** If the Pi is connected to your Home WiFi, it runs ffmpeg in HLS streaming mode, constantly overwriting a small RAM disk. You can view the live stream via the built-in Flask server at http://\<PI\_IP\>:5000/stream.m3u8.  
* **Road Mode:** Once the truck drives away and loses Home WiFi, it immediately switches to continuous recording. It saves fragmented MP4s directly to your permanent storage, organized into daily folders (e.g., /mnt/dashcam/2026-02-26/).

#### **Command-Line Arguments**

The dashcam script can be tweaked on the fly using command-line arguments. This is incredibly useful for testing or altering behavior without editing the Python code.

* \--ignore-parked: Forces the camera into "Road Mode" (saving MP4s to disk) regardless of WiFi connection. Great for bench testing in the garage.  
* \--chunk-seconds \<int\>: Changes the length of the video files. Default is 900 (15 minutes).  
* \--hw-buffer \<int\>: The amount of time (in seconds) the script pauses between chunks to allow the Linux kernel to release the camera hardware. Default is 2\.

#### **Usage Examples:**

Run normally (15-minute chunks, honors WiFi Geofence):

python3 dashcam.py

Force recording to disk while parked in the garage:

python3 dashcam.py \--ignore-parked

Force recording, but save smaller 5-minute (300 seconds) chunks, with a safer 5-second hardware release buffer:

python3 dashcam.py \--ignore-parked \--chunk-seconds 300 \--hw-buffer 5

## **🚀 Running as System Services**

For the best experience, both scripts should be configured as systemd services so they start automatically when the Pi boots.

*(Note: telemetry.py will automatically start and stop the dashcam service as the truck wakes up and goes to sleep, so only telemetry.service needs to be enabled at boot).*
