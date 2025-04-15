#!/bin/bash

# Script to reset SIM800L module via GPIO and then reboot the Raspberry Pi.
# Must be run with sufficient privileges (e.g., using sudo or as root)
# Requires RPi.GPIO library for Python 3 to be installed.

RESET_PIN=21 # GPIO pin connected to SIM800L Reset (using BCM numbering)
PYTHON_EXEC="python3" # Command to run your Python 3 interpreter

echo "--- FrostWarner Manual Reboot Script ---"
echo "$(date): Initiating SIM800L reset (GPIO ${RESET_PIN})..."

# Use Python to perform the GPIO toggle for consistency with your main script.
# The '-c' flag allows running a short Python command string.
${PYTHON_EXEC} -c "
import RPi.GPIO as GPIO
import time
import sys

RESET_PIN = ${RESET_PIN}
print(f'Using GPIO pin {RESET_PIN} (BCM mode)')

try:
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False) # Suppress channel already in use warnings if run multiple times
    GPIO.setup(RESET_PIN, GPIO.OUT)

    # Ensure it starts HIGH (inactive) before pulling LOW
    print('Setting HIGH (initial state)')
    GPIO.output(RESET_PIN, GPIO.HIGH)
    time.sleep(0.5)

    # Perform the reset pulse
    print('Setting LOW (activate reset)')
    GPIO.output(RESET_PIN, GPIO.LOW)
    time.sleep(1) # Hold reset low for 1 second
    print('Setting HIGH (deactivate reset)')
    GPIO.output(RESET_PIN, GPIO.HIGH)
    print('Reset pulse sent. Waiting 5s for modem...')
    time.sleep(5) # Allow time for the module to start booting up

    # Optional: Clean up the GPIO pin we used
    GPIO.cleanup(RESET_PIN)
    print('SIM800L reset sequence completed successfully.')
    sys.exit(0) # Exit Python script with success code

except Exception as e:
    print(f'ERROR during SIM800L reset: {e}', file=sys.stderr)
    # Attempt cleanup even on error
    try:
        GPIO.cleanup(RESET_PIN)
    except:
        pass # Ignore cleanup errors if primary error occurred
    sys.exit(1) # Exit Python script with failure code
"

# Check the exit status of the Python command execution ($?)
# 0 means success, non-zero means failure.
if [ $? -ne 0 ]; then
    echo "$(date): ERROR: Failed to reset SIM800L module. Aborting reboot." | tee -a /home/pi/reboot.log # Log error too
    exit 1 # Stop the script
fi

echo "$(date): SIM800L reset successful."
echo "$(date): Proceeding with system reboot NOW."
sleep 2 # Short pause to allow messages to be seen/logged

# Initiate the reboot. This command requires root privileges.
/sbin/reboot

# If the script reaches here, the reboot command likely failed.
echo "$(date): ERROR: Reboot command failed. Do you have root privileges?" | tee -a /home/pi/reboot.log
exit 1
