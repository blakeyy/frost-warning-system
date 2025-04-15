#!/usr/bin/env python3
import time
import RPi.GPIO as GPIO
import subprocess
import logging
import os

# --- Configuration ---
RESET_PIN = 21                 # GPIO pin for SIM800L Reset
CHECK_INTERVAL_SECONDS = 120   # Check every 2 minutes
FAILURE_THRESHOLD = 3          # Trigger reset after 3 consecutive failures
PING_TARGET = "8.8.8.8"        # Reliable internet IP to ping
PPP_INTERFACE = "ppp0"         # Network interface name for GPRS
PPP_SERVICE_NAME = "ppp-gprs.service"
LOG_FILE = "/home/pi/sim800l_watchdog.log"
INITIAL_DELAY_SECONDS = 60     # Wait after boot before starting checks
MODEM_BOOT_TIME_SECONDS = 15   # Wait after reset before restarting ppp

# --- Logging Setup ---
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
# Add console logging as well for immediate feedback if run manually
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
logging.getLogger().addHandler(console_handler)

# --- GPIO Setup ---
def setup_gpio():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(RESET_PIN, GPIO.OUT)
    # Ensure RESET is initially HIGH (inactive state for SIM800L)
    GPIO.output(RESET_PIN, GPIO.HIGH)
    logging.info(f"GPIO {RESET_PIN} initialized for SIM800L reset.")

# --- Hardware Reset Function ---
def perform_hardware_reset():
    logging.warning("!!! Performing SIM800L Hardware Reset !!!")
    try:
        logging.info(f"Setting GPIO {RESET_PIN} LOW...")
        GPIO.output(RESET_PIN, GPIO.LOW)
        time.sleep(1)  # Hold reset low for 1 second
        logging.info(f"Setting GPIO {RESET_PIN} HIGH...")
        GPIO.output(RESET_PIN, GPIO.HIGH)
        logging.info(f"Hardware reset pulse sent. Waiting {MODEM_BOOT_TIME_SECONDS}s for modem boot...")
        time.sleep(MODEM_BOOT_TIME_SECONDS)
        logging.info("Modem should have rebooted.")
        return True
    except Exception as e:
        logging.error(f"Error during GPIO hardware reset: {e}")
        return False

# --- Check Connectivity Function ---
def check_connectivity():
    """Checks if the PPP interface exists and can ping the target."""
    # 1. Check if interface exists
    try:
        result = subprocess.run(['ip', 'link', 'show', PPP_INTERFACE], capture_output=True, text=True, check=False, timeout=5)
        if result.returncode != 0 or PPP_INTERFACE not in result.stdout:
            logging.warning(f"PPP Interface '{PPP_INTERFACE}' not found.")
            return False
    except subprocess.TimeoutExpired:
         logging.error(f"Timeout checking for interface '{PPP_INTERFACE}'.")
         return False
    except Exception as e:
        logging.error(f"Error checking interface '{PPP_INTERFACE}': {e}")
        return False

    # 2. Try to ping through the interface
    logging.info(f"Pinging {PING_TARGET} via {PPP_INTERFACE}...")
    try:
        # The '-c 1' sends one packet, '-W 5' waits 5 seconds for reply
        # '-I {PPP_INTERFACE}' binds the ping to that specific interface
        result = subprocess.run(
            ['ping', '-c', '1', '-W', '5', '-I', PPP_INTERFACE, PING_TARGET],
            capture_output=True, check=False, timeout=10 # Timeout for the whole command
        )
        if result.returncode == 0:
            logging.info(f"Ping successful to {PING_TARGET} via {PPP_INTERFACE}.")
            return True
        else:
            logging.warning(f"Ping failed (return code {result.returncode}) to {PING_TARGET} via {PPP_INTERFACE}.")
            # Log stderr if available
            if result.stderr:
                logging.warning(f"Ping stderr: {result.stderr.strip()}")
            return False
    except subprocess.TimeoutExpired:
         logging.error(f"Timeout during ping command.")
         return False
    except Exception as e:
        logging.error(f"Error executing ping command: {e}")
        return False

# --- Systemd Service Control ---
def control_service(action):
    """Runs 'sudo systemctl [action] PPP_SERVICE_NAME'."""
    command = ["/usr/bin/sudo", "/bin/systemctl", action, PPP_SERVICE_NAME]
    logging.info(f"Executing: {' '.join(command)}")
    try:
        # Use a longer timeout for stop/start/restart
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)
        if result.returncode != 0:
            logging.error(f"Command failed (code {result.returncode}): {' '.join(command)}")
            logging.error(f"stdout: {result.stdout.strip()}")
            logging.error(f"stderr: {result.stderr.strip()}")
            return False
        logging.info(f"Command successful: {' '.join(command)}")
        return True
    except subprocess.TimeoutExpired:
        logging.error(f"Timeout executing: {' '.join(command)}")
        return False
    except Exception as e:
        logging.error(f"Error executing systemctl command: {e}")
        return False

# --- Main Watchdog Loop ---
def main():
    setup_gpio()
    logging.info("SIM800L Watchdog started.")
    logging.info(f"Initial delay of {INITIAL_DELAY_SECONDS} seconds...")
    time.sleep(INITIAL_DELAY_SECONDS)

    consecutive_failures = 0

    while True:
        try:
            if check_connectivity():
                if consecutive_failures > 0:
                     logging.info("Connectivity restored.")
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                logging.warning(f"Connectivity check failed ({consecutive_failures}/{FAILURE_THRESHOLD}).")

                if consecutive_failures >= FAILURE_THRESHOLD:
                    logging.warning(f"Failure threshold ({FAILURE_THRESHOLD}) reached. Initiating reset sequence.")

                    # 1. Stop the PPP service
                    control_service("stop")
                    time.sleep(2) # Give it a moment to stop

                    # 2. Perform Hardware Reset
                    if perform_hardware_reset():
                         # 3. Start the PPP service again
                         control_service("start")
                    else:
                         logging.error("Hardware reset failed. Cannot restart PPP service.")
                         # Maybe try again later? Or requires manual intervention?
                         # For now, just log and continue loop after interval.

                    # Reset counter regardless of reset success to avoid immediate re-trigger
                    consecutive_failures = 0
                    # Wait a bit longer after a reset attempt before next check
                    logging.info(f"Waiting extra 60s after reset attempt...")
                    time.sleep(60)

            logging.debug(f"Next check in {CHECK_INTERVAL_SECONDS} seconds...")
            time.sleep(CHECK_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            logging.info("Watchdog stopped by user.")
            break
        except Exception as e:
            logging.error(f"Unhandled error in main loop: {e}", exc_info=True)
            # Avoid rapid looping on unexpected errors
            time.sleep(60)

    GPIO.cleanup()
    logging.info("Watchdog finished.")

if __name__ == "__main__":
    main()
