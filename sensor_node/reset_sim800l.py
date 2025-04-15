import RPi.GPIO as GPIO
import time

# Set up GPIO
GPIO.setmode(GPIO.BCM)
RESET_PIN = 21  # Using GPIO 21 for reset
GPIO.setup(RESET_PIN, GPIO.OUT)

# Function to reset the SIM800L
def reset_sim800l():
    # Pull the reset pin LOW
    GPIO.output(RESET_PIN, GPIO.LOW)
    # Wait for a moment
    time.sleep(1)
    # Pull the reset pin HIGH
    GPIO.output(RESET_PIN, GPIO.HIGH)
    # Wait for the module to initialize
    time.sleep(5)
    print("SIM800L has been reset")

# Example usage
reset_sim800l()

# Clean up GPIO when done
GPIO.cleanup()