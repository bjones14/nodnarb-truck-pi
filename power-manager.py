import os
import sys
import time
import json
import logging

LOG_FILE = '/home/brandon/telemetry_debug.log'
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

sys.dont_write_bytecode = True

# --- RECONFIGURED PARAMETERS ---
AWAKE_THRESHOLD_V = 12.5       # Ignition voltage to detect engine running
IGNITION_OFF_V = 2.0           # Static floor to verify engine is off

# --- AMPERAGE DRIVEN PROTOCOLS ---
NET_DISCHARGE_THRESHOLD_A = -0.10  # Any draw worse than -100mA counts as depletion
CHARGER_DISCONNECT_DELAY_S = 180  # Must continuously deplete for 3 mins to exit garage mode
TELEMETRY_WINDOW_S = 300          # Standard battery-only run time limit

TELEMETRY_JSON = "/dev/shm/telemetry.json"
POWER_STATE_JSON = "/dev/shm/power_state.json"

class PowerManager:
    """Manages power states using net current integration for garage tracking."""

    def __init__(self):
        self.boot_time = time.monotonic()
        self.low_ign_start_time = None
        self.continuous_discharge_start = None

        self.is_telemetry_mode = True
        self.is_charging_mode = False
        self.shutdown_triggered = False

    def update(self, current_ign_v, current_main_v, current_amps):
        """Evaluates system state using voltage for ignition and current for charging."""
        # ESCAPE HATCH: If a shutdown was already fired, freeze the state at PENDING
        if self.shutdown_triggered:
            return True, "SHUTDOWN_PENDING"

        now = time.monotonic()

        # 1. STATE: DRIVING (Ignition hot)
        if current_ign_v > AWAKE_THRESHOLD_V:
            self.is_telemetry_mode = False
            self.is_charging_mode = False
            self.low_ign_start_time = None
            self.continuous_discharge_start = None
            return False, "DRIVING"

        # Track when the engine explicitly cut out
        if self.low_ign_start_time is None and current_ign_v < IGNITION_OFF_V:
            self.low_ign_start_time = now

        # 2. EVALUATE NET ELECTRON TRAFFIC
        if current_amps >= NET_DISCHARGE_THRESHOLD_A:
            self.continuous_discharge_start = None

            if current_ign_v < IGNITION_OFF_V:
                if not self.is_charging_mode:
                    logging.info(f"PowerManager: Net current positive/stable ({current_amps}A). Entering Garage Mode.")
                self.is_charging_mode = True
                return False, "CHARGING"
        else:
            if self.continuous_discharge_start is None:
                self.continuous_discharge_start = now

        # 3. STATE: CHARGING / GARAGE MODE (Evaluating Disconnect)
        if self.is_charging_mode:
            elapsed_discharge = now - self.continuous_discharge_start if self.continuous_discharge_start else 0

            if elapsed_discharge > CHARGER_DISCONNECT_DELAY_S:
                logging.warning(f"PowerManager: Sustained depletion detected ({current_amps}A for {elapsed_discharge:.1f}s). Charger disconnected.")
                self.is_charging_mode = False
                return True, "SHUTDOWN_PENDING"

            return False, "CHARGING"

        # 4. STATE: STANDARD TELEMETRY (On Battery Power)
        if self.is_telemetry_mode:
            if (now - self.boot_time) > TELEMETRY_WINDOW_S:
                return True, "SLEEPING"
            return False, "TELEMETRY"

        # 5. STATE: STANDARD SHUTDOWN DEBOUNCE (Engine just turned off, no charger)
        elapsed_off = now - self.low_ign_start_time if self.low_ign_start_time else 0
        if elapsed_off > 120:  # 2 minute grace window before kill
            return True, "SHUTDOWN_PENDING"

        return False, "SHUTDOWN_DEBOUNCE"

    def trigger_shutdown(self):
        """Aggressively signals the OS to power off using multiple privilege strategies."""
        if not self.shutdown_triggered:
            logging.warning("SYSTEM SHUTDOWN INITIATED BY CURRENT BALANCING AUDIT.")
            self.shutdown_triggered = True

        # We execute these every loop cycle. If Polkit blocks one, the next fallback will trip.
        logging.debug("PowerManager: Executing system poweroff commands...")
        os.system("systemctl poweroff")
        os.system("sudo systemctl poweroff")
        os.system("poweroff")
        os.system("sudo poweroff")


def main():
    logging.info("Power Manager Microservice Initialized (Amperage Tracking Enhanced).")
    pwr_manager = PowerManager()

    while True:
        try:
            ign_v = 0.0
            main_v = 0.0
            amps = 0.0

            if os.path.exists(TELEMETRY_JSON):
                with open(TELEMETRY_JSON, "r") as f:
                    data = json.load(f)
                    ign_v = data.get("ign_voltage", 0.0)
                    main_v = data.get("battery_voltage", 0.0)
                    amps = data.get("current_amps", 0.0)
            else:
                time.sleep(1)
                continue

            should_shutdown, pwr_state = pwr_manager.update(ign_v, main_v, amps)

            with open(POWER_STATE_JSON, "w") as f:
                json.dump({"power_state": pwr_state}, f)

            if should_shutdown:
                pwr_manager.trigger_shutdown()

        except Exception as e:
            logging.error(f"Power Manager Loop Error: {e}")

        time.sleep(1)


if __name__ == "__main__":
    main()

