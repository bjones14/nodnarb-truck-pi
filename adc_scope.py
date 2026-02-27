import time
import board
import busio
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn

# --- CALIBRATION ---
DIVIDER_FACTOR = 5.545
CURRENT_SENSITIVITY = 0.003125 # V per Amp
CURRENT_ZERO_OFFSET = 0.0074   # Your baseline offset

def main():
    print("Initializing I2C Bus and ADS1115...")
    try:
        # Initialize the I2C interface
        i2c = busio.I2C(board.SCL, board.SDA)

        # Create the ADC object using the I2C bus
        ads = ADS.ADS1115(i2c)
        ads.gain = 1 # Gain 1 = +/- 4.096V range

        # Create single-ended input on channels (using raw integers to bypass library bug)
        chan_a0 = AnalogIn(ads, 0) # Main Battery (F12)
        chan_a1 = AnalogIn(ads, 1) # Ignition (F26)
        chan_a2 = AnalogIn(ads, 2) # Hall Sensor Vout (Yellow)
        chan_a3 = AnalogIn(ads, 3) # Hall Sensor Vref (Green/White)

    except Exception as e:
        print(f"Failed to initialize hardware: {e}")
        return

    print("\n--- LIVE ADC SCOPE ---")
    print("Press Ctrl+C to exit.\n")
    print(f"{'A0 (Batt)':<12} | {'A1 (Ign)':<12} | {'A2 (Vout)':<12} | {'A3 (Vref)':<12} | {'DIFF (Vout-Vref)':<18} | {'CALC AMPS':<10}")
    print("-" * 85)

    try:
        while True:
            # Read Raw Voltages
            v_a0 = chan_a0.voltage
            v_a1 = chan_a1.voltage
            v_a2 = chan_a2.voltage
            v_a3 = chan_a3.voltage

            # Calculate 12V equivalents
            batt_v = v_a0 * DIVIDER_FACTOR
            ign_v  = v_a1 * DIVIDER_FACTOR

            # Calculate Current
            raw_diff = v_a2 - v_a3
            corrected_diff = raw_diff - CURRENT_ZERO_OFFSET
            amps = corrected_diff / CURRENT_SENSITIVITY

            # Format output strings
            s_a0 = f"{v_a0:.3f}V ({batt_v:.1f}V)"
            s_a1 = f"{v_a1:.3f}V ({ign_v:.1f}V)"
            s_a2 = f"{v_a2:.3f}V"
            s_a3 = f"{v_a3:.3f}V"
            s_diff = f"{raw_diff:.4f}V"
            s_amps = f"{amps:.2f} A"

            # Print row
            print(f"{s_a0:<12} | {s_a1:<12} | {s_a2:<12} | {s_a3:<12} | {s_diff:<18} | {s_amps:<10}", end='\r')

            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\n\nExiting scope...")

if __name__ == "__main__":
    main()

