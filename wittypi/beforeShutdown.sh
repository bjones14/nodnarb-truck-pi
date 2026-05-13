#!/bin/bash
# file: beforeShutdown.sh
#
# This script will be executed after Witty Pi receives shutdown command (GPIO-4 gets pulled down).
# If you want to run your commands before turnning of your Raspberry Pi, you can place them here.
# Raspberry Pi will not shutdown until all commands here are executed.
#
# Remarks: please use absolute path of the command, or it can not be found (by root user).
# Remarks: you may append '&' at the end of command to avoid blocking the main daemon.sh.
#
# --- DIAGNOSTIC LOGGING ---
# Helpful for verifying the sequence in your debug logs
LOG_FILE="/home/brandon/telemetry_debug.log"
echo "[$(date)] beforeShutdown.sh: Shutdown signal received from Witty Pi." >> "$LOG_FILE"

# 1. STOP THE DASHCAM SERVICE
# This sends the SIGTERM signal to your Python script, triggering 
# the graceful finalize logic we added.
echo "[$(date)] beforeShutdown.sh: Stopping dashcam service..." >> "$LOG_FILE"
/usr/bin/systemctl stop dashcam.service

# 2. STOP THE TELEMETRY SERVICE
# This ensures your BatteryTracker saves the last known SoC to soc_state.json
echo "[$(date)] beforeShutdown.sh: Stopping telemetry service..." >> "$LOG_FILE"
/usr/bin/systemctl stop telemetry.service

# 3. GRACE PERIOD
# Give the scripts a few seconds to finalize FFmpeg and MQTT connections.
/usr/bin/sleep 5

# 4. FLUSH DISK BUFFERS
# The most important step for an SD card-based system. This forces
# all cached data to be physically written to the storage.
echo "[$(date)] beforeShutdown.sh: Syncing filesystems..." >> "$LOG_FILE"
/usr/bin/sync

echo "[$(date)] beforeShutdown.sh: Pre-shutdown tasks complete. System will now halt." >> "$LOG_FILE"
