#!/usr/bin/env python3
"""
Integriertes Frostwarnsystem mit MQTT-Anbindung

Dieses Skript überwacht kontinuierlich die Temperaturen, sendet Daten
und Status-Updates über MQTT an einen Broker. Es nutzt MQTT Last Will
and Testament (LWT) und Graceful Shutdown für eine zuverlässige Statusanzeige.

Funktionen:
- Temperaturüberwachung (Trocken- und Nasstemperatur)
- Luftfeuchtigkeitsmessung
- Batteriespannungs- und DC-DC-Ausgangsspannungsüberwachung
- MQTT-Datenübertragung (Sensordaten, Status)
- Zuverlässige Online-/Offline-Statusmeldung via MQTT
- Datenaufzeichnung (lokal als CSV)
- Datenpufferung bei MQTT-Verbindungsverlust
- Systeminformationen (CPU, RAM, Disk, Uptime)
- Konfigurierbare Schwellwerte und Intervalle
- Spannungskalibrierung
"""

import time
import os
import glob
# import serial # REMOVED: No longer needed for SMS/GSM
import RPi.GPIO as GPIO
import adafruit_dht
import board
import busio
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn
from datetime import datetime, timezone # Added timezone
import subprocess # Still used for reboot? Check usage. -> Yes, used for reboot. Keep.
import math
import threading
import json
import psutil
import logging
import paho.mqtt.client as mqtt
import uuid
import socket
import signal # ADDED: For graceful shutdown
import sys    # ADDED: For graceful shutdown and exit codes

# Logging einrichten
logging.basicConfig(
    filename='/home/pi/frost_system_mqtt.log', # Changed log filename
    level=logging.INFO, # Changed default level to INFO, DEBUG is very verbose
    format='%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s' # Added thread name
)

# Konfiguration
CONFIG_FILE = "/home/pi/frost_config_mqtt.json" # Changed config filename
LOG_FILE = "/home/pi/temp_log_mqtt.csv"         # Changed data log filename
DATA_BUFFER_FILE = "/home/pi/unsent_data_mqtt.json" # Changed buffer filename

DEFAULT_CONFIG = {
    # --- Core Settings ---
    "warning_temp": 0.0,                    # Warnschwellwert in °C (still relevant for logging/internal logic)
    "check_interval": 900,                  # 15 Minuten (normal sensor check)
    "check_interval_critical": 300,         # 5 Minuten (sensor check below warning_temp)

    # --- Battery Monitoring ---
    "battery_warning_level": 20,            # Batteriewarnung in Prozent
    "battery_critical_level": 10,           # Kritischer Batteriestand in Prozent
    "battery_calibration_factor": 1.0,      # Kalibrierungsfaktor für Batteriespannung
    "battery_r1": 82000,                    # R1 Widerstand im Spannungsteiler (Ohm)
    "battery_r2": 10000,                    # R2 Widerstand im Spannungsteiler (Ohm)
    "dcdc_calibration_factor": 1.0,         # Kalibrierungsfaktor für DC-DC Ausgangsspannung
    "dcdc_r1": 10000,                       # R1 Widerstand im Spannungsteiler für DC-DC (Ohm)
    "dcdc_r2": 10000,                       # R2 Widerstand im Spannungsteiler für DC-DC (Ohm)

    # --- MQTT Configuration ---
    "mqtt_broker": "YOUR_SERVER_IP",        # <<< CHANGE THIS
    "mqtt_port": 1883,
    "mqtt_username": "",
    "mqtt_password": "",
    "mqtt_sensor_topic_template": "frostsystem/{device_id}/sensors", # Topic for sensor data
    "mqtt_status_topic_template": "frostsystem/{device_id}/status",   # Topic for status updates (online/offline)
    "mqtt_command_topic_template": "frostsystem/{device_id}/cmd",     # Topic to listen for commands (Future Use)
    "mqtt_qos": 1,                          # QoS for reliable messaging
    "mqtt_keepalive": 60,                   # Keepalive interval for connection check
    "mqtt_status_heartbeat_interval": 300,  # Interval (sec) to send "online" status heartbeat
    "device_id": str(uuid.uuid4()),         # Auto-generate if not present
    "max_buffer_size": 1000                 # Increased buffer size
}

# --- REMOVED SMS Configuration Keys ---
# "authorized_numbers", "sms_check_interval", "status_code",
# "threshold_code", "reboot_code", "add_number_code",
# "remove_number_code", "help_code"

# Globale Variablen
config = {}
last_readings = {
    "dry_temp": None,
    "wet_temp": None,
    "humidity": None,
    "calc_wet_temp": None,
    "battery_percent": None,
    "battery_voltage": None,
    "dcdc_voltage": None,
    "last_update": None
}
shutdown_requested = False # Flag for graceful shutdown


# Thread-Synchronisierung
sensor_lock = threading.Lock()
# gsm_lock = threading.Lock() # REMOVED: No longer needed
buffer_lock = threading.Lock()  # For the unsent data buffer

# MQTT Client Global Variables
mqtt_client = None
mqtt_connected = False
mqtt_lock = threading.Lock()  # Lock for MQTT operations
device_id = ""  # Will be loaded from config

# Unsent data buffer
unsent_data_buffer = []

# Batterie-Monitoring Variablen
adc = None
battery_channel = None
dcdc_channel = None

# GPIO initialisieren
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

# --- REMOVED SIM800L Reset Pin Setup ---
# RESET_PIN = 21
# GPIO.setup(RESET_PIN, GPIO.OUT)
# GPIO.output(RESET_PIN, GPIO.HIGH)

# 1-Wire für DS18B20 initialisieren
try:
    os.system('modprobe w1-gpio')
    os.system('modprobe w1-therm')
    base_dir = '/sys/bus/w1/devices/'
    device_folders = glob.glob(base_dir + '28*')
    device_files = [folder + '/w1_slave' for folder in device_folders]
    if len(device_files) < 2:
         logging.warning(f"Nur {len(device_files)} DS18B20 Sensoren gefunden. Benötige 2.")
         # Assign what we have, handle None later
         DRY_SENSOR = device_files[0] if len(device_files) > 0 else None
         WET_SENSOR = None
    else:
        # Assuming order based on connection/discovery
        DRY_SENSOR = device_files[0]
        WET_SENSOR = device_files[1]
        logging.info(f"DS18B20 Sensoren gefunden: Trocken={DRY_SENSOR}, Nass={WET_SENSOR}")

except Exception as e:
    logging.error(f"Fehler bei Initialisierung der DS18B20 Sensoren: {e}")
    DRY_SENSOR = None
    WET_SENSOR = None


# DHT22 Sensor global variable
DHT_SENSOR = None

# --- REMOVED GSM Initialization ---
# try:
#     gsm = None # serial.Serial('/dev/ttyS0', 9600, timeout=1) # Keep None
# except Exception as e:
#     gsm = None
#     logging.error(f"GSM-Modul konnte nicht initialisiert werden: {e}")


# --- REMOVED reset_sim800l function ---

def init_dht_sensor():
    """Initialize the DHT22 sensor with improved configuration"""
    global DHT_SENSOR

    try:
        # Import the more reliable CircuitPython library if available
        try:
            import adafruit_dht
            import board
            # Configure GPIO pin with pull-up (Good practice)
            # Pin 17 is GPIO17 (BCM numbering)
            DHT_PIN = 17
            GPIO.setup(DHT_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            time.sleep(0.1) # Allow pull-up to stabilize
            DHT_SENSOR = adafruit_dht.DHT22(board.D17, use_pulseio=False) # Explicitly disable pulseio if causing issues
            logging.info("DHT22 initialized with CircuitPython library (GPIO 17)")
        except ImportError:
            logging.warning("CircuitPython DHT library not available. DHT functionality limited.")
            DHT_SENSOR = None
            return False
        except RuntimeError as error:
            # Catch common init errors like "RuntimeError: DHT sensor not found"
             logging.error(f"DHT22 initialization error (CircuitPython): {error}")
             DHT_SENSOR = None
             return False

        return True
    except Exception as e:
        logging.error(f"General DHT22 initialization error: {e}")
        DHT_SENSOR = None
        return False

def reset_dht_sensor():
    """Reset the DHT22 sensor GPIO pin when it fails repeatedly"""
    # Note: This is experimental and might not always work.
    try:
        pin = 17  # DHT22 pin (BCM)
        logging.warning("Attempting DHT22 sensor GPIO reset...")
        # Set pin as output and cycle it
        GPIO.setup(pin, GPIO.OUT)
        GPIO.output(pin, GPIO.LOW)
        time.sleep(0.5)
        GPIO.output(pin, GPIO.HIGH)
        time.sleep(0.5)

        # Return to input mode with pull-up
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        time.sleep(2) # Give sensor time to stabilize after reset

        # Re-initialize the sensor object
        return init_dht_sensor()
    except Exception as e:
        logging.error(f"Error resetting DHT22 sensor: {e}")
        return False

# ADS1115 für Batteriespannungsmessung initialisieren
def init_battery_monitor():
    """Initialisiere den ADS1115 für die Batteriespannungsmessung"""
    global adc, battery_channel, dcdc_channel

    try:
        # Initialisiere I2C-Bus
        i2c = busio.I2C(board.SCL, board.SDA)

        # Erstelle ADS-Objekt an Standardadresse 0x48
        adc = ADS.ADS1115(i2c, address=0x48) # Make sure address is correct

        # Setze Gain auf 1 für einen Messbereich von ±4.096V
        # This is suitable if V_measured * R2 / (R1+R2) < 4.096V
        adc.gain = 1

        # Single-ended Eingang an Kanal 0 für Batterie
        battery_channel = AnalogIn(adc, ADS.P0)

        # Single-ended Eingang an Kanal 1 für DC-DC Ausgang
        dcdc_channel = AnalogIn(adc, ADS.P1)

        logging.info("ADS1115 für Spannungsmessung initialisiert (Gain=1, Addr=0x48)")
        return True
    except ValueError as e:
         # Common error if sensor not found at address
         logging.error(f"Fehler bei der Initialisierung des ADS1115 (Addr 0x48): Sensor nicht gefunden oder I2C Problem? {e}")
         return False
    except Exception as e:
        logging.error(f"Allgemeiner Fehler bei der Initialisierung des ADS1115: {e}")
        return False

# Hilfsfunktionen

def load_config():
    """Lädt die Konfiguration aus der Datei oder erstellt Standardwerte"""
    global config, device_id
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                loaded_config = json.load(f)
                # Merge loaded config with defaults, ensuring all keys exist
                config = {**DEFAULT_CONFIG, **loaded_config}
                # Ensure Device ID exists and is valid
                if "device_id" not in config or not config["device_id"] or not isinstance(config["device_id"], str):
                    logging.warning("Device ID missing or invalid in config, generating new one.")
                    config["device_id"] = str(uuid.uuid4())
                    save_config() # Save the newly generated ID
                logging.info("Konfiguration geladen")
        else:
            config = DEFAULT_CONFIG.copy()
            # Ensure a device ID is generated for new default config
            if "device_id" not in config or not config["device_id"]:
                 config["device_id"] = str(uuid.uuid4())
            save_config()
            logging.info("Standardkonfiguration erstellt")
    except json.JSONDecodeError as e:
         logging.error(f"Fehler beim Parsen der Konfigurationsdatei {CONFIG_FILE}: {e}. Verwende Standardkonfiguration.")
         config = DEFAULT_CONFIG.copy()
         if "device_id" not in config or not config["device_id"]:
              config["device_id"] = str(uuid.uuid4())
    except Exception as e:
        logging.error(f"Fehler beim Laden der Konfiguration: {e}. Verwende Standardkonfiguration.")
        config = DEFAULT_CONFIG.copy()
        if "device_id" not in config or not config["device_id"]:
             config["device_id"] = str(uuid.uuid4())
    finally:
        # Ensure device_id global is set from the final config
        device_id = config.get("device_id", str(uuid.uuid4())) # Fallback just in case
        logging.info(f"Verwende Device ID: {device_id}")


def save_config():
    """Speichert die aktuelle Konfiguration in die Datei"""
    global config
    try:
        # Ensure device ID is in the config being saved
        config['device_id'] = device_id
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=4, sort_keys=True) # Sort keys for readability
        logging.info("Konfiguration gespeichert")
    except Exception as e:
        logging.error(f"Fehler beim Speichern der Konfiguration: {e}")

# --- REMOVED send_command function ---
# --- REMOVED send_sms function ---
# --- REMOVED read_sms function ---
# --- REMOVED process_sms_commands function ---
# --- REMOVED sms_service_loop function ---

def fmt(val, prec=2):
    """Format helper for logging, handles None."""
    return f"{val:.{prec}f}" if val is not None else "N/A"

# --- REMOVED replace_special_chars function ---

def read_temp_raw(device_file):
    """Liest die Rohdaten vom Temperatursensor"""
    try:
        with open(device_file, 'r') as f:
            lines = f.readlines()
        return lines
    except FileNotFoundError:
        logging.error(f"Temperatursensor-Datei nicht gefunden: {device_file}")
        return None
    except Exception as e:
        logging.error(f"Fehler beim Lesen der Temperatursensor-Datei {device_file}: {e}")
        return None

def read_temp(device_file):
    """Liest die Temperatur vom DS18B20 Sensor"""
    if not device_file:
        logging.debug("Keine DS18B20 Gerätedatei zum Lesen angegeben.")
        return None

    lines = None
    retries = 3 # Retry reading a few times
    for attempt in range(retries):
         lines = read_temp_raw(device_file)
         if lines and len(lines) >= 2 and lines[0].strip().endswith('YES'):
             break # Got a valid reading
         elif lines and len(lines) >= 2:
             logging.debug(f"DS18B20 CRC Check failed for {device_file}. Versuch {attempt+1}/{retries}")
             time.sleep(0.3) # Wait before retry
         elif not lines:
             logging.warning(f"Konnte DS18B20-Datei nicht lesen {device_file}. Versuch {attempt+1}/{retries}")
             time.sleep(0.5) # Wait longer if file read failed
         else: # Should not happen if lines has content
              logging.warning(f"Unerwarteter Zustand beim Lesen von {device_file}. Versuch {attempt+1}/{retries}")
              time.sleep(0.3)


    if not lines or len(lines) < 2 or not lines[0].strip().endswith('YES'):
        logging.error(f"Konnte keine gültigen Daten von DS18B20 {device_file} nach {retries} Versuchen lesen.")
        return None

    try:
        equals_pos = lines[1].find('t=')
        if equals_pos != -1:
            temp_string = lines[1][equals_pos+2:]
            temp_c = float(temp_string) / 1000.0
            # Basic sanity check
            if -55 < temp_c < 125:
                 return temp_c
            else:
                 logging.warning(f"Unplausibler Temperaturwert von {device_file}: {temp_c}°C")
                 return None
        else:
            logging.error(f"Formatfehler in DS18B20 Daten von {device_file}: {lines[1]}")
            return None
    except ValueError as e:
        logging.error(f"Fehler beim Konvertieren der Temperatur von {device_file}: {e} (String: '{temp_string}')")
        return None
    except Exception as e:
        logging.error(f"Allgemeiner Fehler beim Parsen der Temperatur von {device_file}: {e}")
        return None


def read_humidity():
    """Improved humidity reading function with auto-reset capability"""
    global DHT_SENSOR
    if not DHT_SENSOR:
        logging.debug("DHT Sensor Objekt nicht initialisiert.")
        if not init_dht_sensor(): # Try to init again
            logging.warning("DHT Sensor konnte nicht initialisiert werden, keine Feuchtigkeitsmessung.")
            return None
        # If init succeeded, DHT_SENSOR is now set

    max_retries = 5
    retry_delay = 2 # seconds between reads
    reset_attempted = False

    for retry in range(max_retries):
        try:
            # Using CircuitPython library
            humidity = DHT_SENSOR.humidity
            # Sometimes reads 0.0% which might be unrealistic depending on environment
            if humidity is not None and 0 <= humidity <= 100:
                logging.debug(f"DHT22 Feuchtigkeit gelesen: {humidity:.1f}%")
                return humidity
            else:
                 logging.debug(f"DHT22 gab ungültige Feuchtigkeit zurück ({humidity}), Versuch {retry+1}/{max_retries}")

        except RuntimeError as error:
            # RuntimeErrors are common if sensor doesn't respond
            logging.debug(f"DHT22 Lesefehler: {error}, Versuch {retry+1}/{max_retries}")
            # After 3 failed attempts, try resetting the sensor *once*
            if retry >= 2 and not reset_attempted:
                reset_attempted = True # Ensure reset is only tried once per read_humidity call
                if reset_dht_sensor():
                    logging.info("DHT22 Sensor Reset erfolgreich, versuche erneut zu lesen.")
                    # Give it a bit more time after reset
                    time.sleep(3)
                    # Continue to the next iteration to retry reading
                    continue
                else:
                    logging.error("DHT22 Sensor Reset fehlgeschlagen.")
                    # No point retrying further if reset failed
                    return None # Exit function after failed reset
        except Exception as e:
            # Catch other potential errors
            logging.error(f"Unerwarteter Fehler beim Lesen des DHT22: {e}", exc_info=True)
            # Maybe exit after unexpected error? Or just retry? Let's retry.

        # Wait before the next retry
        time.sleep(retry_delay)

    logging.error(f"DHT22 Lesefehler nach {max_retries} Versuchen.")
    return None


def calculate_wet_bulb(temp, humidity):
    """Berechnet die Nasstemperatur aus Trockentemperatur und Luftfeuchtigkeit"""
    if temp is None or humidity is None:
        return None

    # Ensure humidity is within a reasonable range (e.g., 0.1% to 100%) for calculation stability
    humidity = max(0.1, min(humidity, 100.0))

    try:
        # Magnus formula for saturation vapor pressure (es) in hPa
        es = 6.112 * math.exp((17.67 * temp) / (temp + 243.5))
        # Actual vapor pressure (e) in hPa
        e = (humidity / 100.0) * es

        # Approximate formula for Wet Bulb Temperature (Tw) - Stull's approximation is often cited
        # Tw = T * atan[0.151977 * (RH% + 8.313659)^(1/2)] + atan(T + RH%) - atan(RH% - 1.676331) + 0.00391838 *(RH%)^(3/2) * atan(0.023101 * RH%) - 4.686035
        # Note: This formula expects RH in % (0-100)
        term1 = temp * math.atan(0.151977 * (humidity + 8.313659)**0.5)
        term2 = math.atan(temp + humidity)
        term3 = math.atan(humidity - 1.676331)
        term4 = 0.00391838 * (humidity**1.5) * math.atan(0.023101 * humidity)
        term5 = 4.686035

        tw = term1 + term2 - term3 + term4 - term5

        # Wet bulb temp should not be higher than dry bulb temp
        tw = min(tw, temp)

        return tw
    except ValueError as e:
        # E.g., math domain error if arguments to atan/sqrt are invalid
         logging.error(f"Fehler bei der Nasstemperaturberechnung (ValueError): {e}. Temp={temp}, Hum={humidity}")
         return None
    except Exception as e:
        logging.error(f"Allgemeiner Fehler bei der Nasstemperaturberechnung: {e}")
        return None

def get_stable_voltage(channel, samples=10, delay=0.05):
    """Liefert einen stabileren Spannungswert durch Mittelwertbildung"""
    if channel is None:
        logging.debug("Kein ADC Kanal für Spannungsmessung angegeben.")
        return None

    readings = []
    try:
        for i in range(samples):
            try:
                 # Read the raw ADC value and the voltage
                 # raw_value = channel.value # 0-65535 for ADS1115
                 voltage = channel.voltage # Calculated voltage based on gain
                 readings.append(voltage)
                 # logging.debug(f"ADC Sample {i+1}/{samples}: Value={raw_value}, Voltage={voltage:.4f}V")
                 time.sleep(delay)
            except Exception as read_err:
                 # Catch potential errors during individual reads (e.g., I2C issue)
                 logging.warning(f"Fehler bei ADC Einzelmessung: {read_err}. Überspringe Sample.")
                 time.sleep(delay * 2) # Wait a bit longer after an error

        if not readings:
             logging.error("Keine gültigen ADC Messwerte erhalten.")
             return None

        # Simple Averaging (consider removing outliers if noise is high)
        if len(readings) > 4: # Only remove outliers if enough samples
             readings.sort()
             # Remove highest and lowest reading
             trimmed_readings = readings[1:-1]
             avg_voltage = sum(trimmed_readings) / len(trimmed_readings)
             logging.debug(f"ADC Spannung (stabilisiert, ohne Ausreißer): {avg_voltage:.4f}V aus {len(trimmed_readings)} Werten.")
        elif readings:
             avg_voltage = sum(readings) / len(readings)
             logging.debug(f"ADC Spannung (stabilisiert): {avg_voltage:.4f}V aus {len(readings)} Werten.")
        else:
            # Should not happen if check above works, but as safety
            logging.error("Konnte keinen Durchschnitt bilden, keine gültigen Messwerte.")
            return None

        return avg_voltage
    except Exception as e:
        logging.error(f"Fehler bei der stabilisierten Spannungsmessung: {e}")
        return None

def get_battery_voltage():
    """Berechne kalibrierte Batteriespannung"""
    global config # Ensure access to latest config
    if not battery_channel:
        return None

    voltage_at_pin = get_stable_voltage(battery_channel)

    if voltage_at_pin is None:
        return None

    # Berechne Spannungsteiler-Verhältnis using values from config
    r1 = config.get('battery_r1', DEFAULT_CONFIG['battery_r1'])
    r2 = config.get('battery_r2', DEFAULT_CONFIG['battery_r2'])
    if r2 <= 0: # Avoid division by zero
         logging.error("Batterie R2 Widerstand ist 0 oder negativ in der Konfiguration.")
         return None
    divider_ratio = (r1 + r2) / r2

    # Wende Kalibrierungsfaktor an
    calibration_factor = config.get('battery_calibration_factor', DEFAULT_CONFIG['battery_calibration_factor'])

    calculated_voltage = voltage_at_pin * divider_ratio * calibration_factor
    logging.debug(f"Batteriespannung Roh={voltage_at_pin:.4f}V, Calc={calculated_voltage:.2f}V (Ratio={divider_ratio:.2f}, Calib={calibration_factor:.4f})")
    return calculated_voltage


def get_dcdc_voltage():
    """Berechne kalibrierte DC-DC Ausgangsspannung"""
    global config # Ensure access to latest config
    if not dcdc_channel:
        return None

    voltage_at_pin = get_stable_voltage(dcdc_channel)

    if voltage_at_pin is None:
        return None

    # Berechne Spannungsteiler-Verhältnis
    r1 = config.get('dcdc_r1', DEFAULT_CONFIG['dcdc_r1'])
    r2 = config.get('dcdc_r2', DEFAULT_CONFIG['dcdc_r2'])
    if r2 <= 0:
         logging.error("DC-DC R2 Widerstand ist 0 oder negativ in der Konfiguration.")
         return None
    divider_ratio = (r1 + r2) / r2

    # Wende Kalibrierungsfaktor an
    calibration_factor = config.get('dcdc_calibration_factor', DEFAULT_CONFIG['dcdc_calibration_factor'])

    calculated_voltage = voltage_at_pin * divider_ratio * calibration_factor
    logging.debug(f"DC-DC Spannung Roh={voltage_at_pin:.4f}V, Calc={calculated_voltage:.2f}V (Ratio={divider_ratio:.2f}, Calib={calibration_factor:.4f})")
    return calculated_voltage

def battery_voltage_to_percent(voltage):
    """Konvertiere Batteriespannung in Prozentwert (Beispiel für 12V Blei-Säure)"""
    if voltage is None:
        return None

    # EXAMPLE for a 12V Lead-Acid Battery (AGM/Gel might differ slightly)
    # These values are approximate and depend heavily on temperature and load.
    # Adjust based on your specific battery datasheet!
    max_voltage = 12.8  # Fully charged (rest voltage) ~100%
    mid_voltage_high = 12.5 # ~75%
    mid_voltage_low = 12.2 # ~50%
    min_voltage_warn = 11.9 # ~25% (Start warning)
    min_voltage_crit = 11.6 # ~10% (Critical, avoid discharge below this)
    min_voltage_empty = 11.0 # Deeply discharged 0% (Avoid reaching this!)


    # Simple non-linear mapping (adjust thresholds as needed)
    if voltage >= max_voltage:
        percent = 100
    elif voltage >= mid_voltage_high: # 12.5 - 12.8
        percent = 75 + (voltage - mid_voltage_high) / (max_voltage - mid_voltage_high) * 25
    elif voltage >= mid_voltage_low: # 12.2 - 12.5
        percent = 50 + (voltage - mid_voltage_low) / (mid_voltage_high - mid_voltage_low) * 25
    elif voltage >= min_voltage_warn: # 11.9 - 12.2
        percent = 25 + (voltage - min_voltage_warn) / (mid_voltage_low - min_voltage_warn) * 25
    elif voltage >= min_voltage_crit: # 11.6 - 11.9
        percent = 10 + (voltage - min_voltage_crit) / (min_voltage_warn - min_voltage_crit) * 15
    elif voltage >= min_voltage_empty: # 11.0 - 11.6
        percent = 0 + (voltage - min_voltage_empty) / (min_voltage_crit - min_voltage_empty) * 10
    else: # Below 11.0V
        percent = 0

    percent = max(0, min(100, int(percent))) # Ensure value is between 0 and 100
    logging.debug(f"Batterie Umrechnung: {voltage:.2f}V -> {percent}%")
    return percent

# --- Calibration functions remain the same, they are useful for setup ---
def calibrate_voltage_sensor(channel, r1, r2, name, config_key):
    """Kalibriere einen Spannungsmesser mit einem bekannten Wert"""
    global config

    if not channel:
         print(f"FEHLER: ADC-Kanal für '{name}' nicht verfügbar.")
         return False

    try:
        print(f"\n--- Kalibrierung für: {name} ---")
        print("Messe Spannung am ADC Pin...")
        voltage_at_pin = get_stable_voltage(channel, samples=20, delay=0.1)
        if voltage_at_pin is None:
            logging.error(f"Konnte keine Spannung am Pin für {name} messen")
            print("FEHLER: Konnte keine Spannung am ADC Pin messen.")
            return False

        # Berechne unkalibrierte Spannung
        if r2 <= 0:
             print(f"FEHLER: R2 Widerstand für {name} ist ungültig ({r2} Ohm).")
             return False
        divider_ratio = (r1 + r2) / r2
        raw_voltage = voltage_at_pin * divider_ratio

        print(f"Gemessene Spannung am ADC Pin: {voltage_at_pin:.4f}V")
        print(f"Berechnete {name} (unkalibriert): {raw_voltage:.2f}V")
        print("-" * 30)
        print(f"Bitte messe jetzt die tatsächliche '{name}' mit einem Multimeter.")

        while True:
            try:
                actual_voltage_str = input(f"Gib die gemessene {name} in Volt ein (z.B. 12.65): ")
                actual_voltage = float(actual_voltage_str)
                if actual_voltage <= 0:
                    print("Ungültige Eingabe. Spannung muss positiv sein.")
                else:
                    break # Valid input
            except ValueError:
                print("Ungültige Eingabe. Bitte eine Zahl eingeben.")

        # Berechne Kalibrierungsfaktor
        if raw_voltage == 0:
             print("FEHLER: Unkalibrierte Spannung ist 0. Kalibrierung nicht möglich.")
             return False
        calibration_factor = actual_voltage / raw_voltage

        # Speichere in Konfiguration
        config[config_key] = calibration_factor
        save_config() # Save immediately

        print("-" * 30)
        print(f"Neuer Kalibrierungsfaktor für {name}: {calibration_factor:.4f}")
        print(f"Konfiguration '{CONFIG_FILE}' wurde aktualisiert.")
        print("--- Kalibrierung abgeschlossen ---")
        return True

    except Exception as e:
        logging.error(f"Fehler bei der Kalibrierung von {name}: {e}")
        print(f"Ein Fehler ist aufgetreten: {e}")
        return False

def calibrate_battery_sensor():
    """Kalibriere den Batteriespannungsmesser"""
    r1 = config.get('battery_r1', DEFAULT_CONFIG['battery_r1'])
    r2 = config.get('battery_r2', DEFAULT_CONFIG['battery_r2'])
    return calibrate_voltage_sensor(
        battery_channel, r1, r2, "Batteriespannung", "battery_calibration_factor"
    )

def calibrate_dcdc_sensor():
    """Kalibriere den DC-DC Ausgangsspannungsmesser"""
    r1 = config.get('dcdc_r1', DEFAULT_CONFIG['dcdc_r1'])
    r2 = config.get('dcdc_r2', DEFAULT_CONFIG['dcdc_r2'])
    return calibrate_voltage_sensor(
        dcdc_channel, r1, r2, "DC-DC Ausgangsspannung", "dcdc_calibration_factor"
    )

# --- Removed check_battery (merged into update_sensor_data) ---

def get_system_info():
    """Sammelt Systeminformationen wie CPU-Last, Speicherverbrauch, etc."""
    try:
        # CPU-Last (average over 1 second for more stability)
        cpu_percent = psutil.cpu_percent(interval=1.0)

        # Speicherverbrauch
        memory = psutil.virtual_memory()
        memory_percent = memory.percent

        # Speicherplatz auf Root-Partition
        disk = psutil.disk_usage('/')
        disk_percent = disk.percent

        # Systemlaufzeit
        boot_time_timestamp = psutil.boot_time()
        current_time_timestamp = time.time()
        uptime_seconds = current_time_timestamp - boot_time_timestamp

        # Format uptime
        days = int(uptime_seconds // (24 * 3600))
        hours = int((uptime_seconds % (24 * 3600)) // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        uptime_str = f"{days}d {hours}h {minutes}m"

        # Get IP address (more robustly)
        ip_address = "N/A"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0) # Non-blocking
            # doesn't have to be reachable
            s.connect(('10.254.254.254', 1))
            ip_address = s.getsockname()[0]
            s.close()
        except Exception:
             # Try getting hostname as fallback
             try:
                 ip_address = socket.gethostname()
             except Exception:
                  pass # Leave as N/A


        sysinfo = {
            "cpu_percent": cpu_percent,
            "memory_percent": memory_percent,
            "disk_percent": disk_percent,
            "uptime_seconds": int(uptime_seconds),
            "uptime_str": uptime_str,
            "ip_address": ip_address
        }
        logging.debug(f"System Info: {sysinfo}")
        return sysinfo
    except Exception as e:
        logging.error(f"Fehler beim Sammeln der Systeminformationen: {e}")
        return None

def log_data(timestamp, data_dict):
    """Daten in CSV-Datei loggen"""
    # Use the provided timestamp and data dictionary

    # Ensure consistent order of columns
    header = [
        "Zeitstempel", "Trockentemperatur", "Nasstemperatur_gemessen",
        "Luftfeuchtigkeit", "Nasstemperatur_berechnet", "Effektive_Nasstemp",
        "Batterie_Prozent", "Batterie_Spannung", "DCDC_Spannung"
    ]
    data_keys = [
        "timestamp", "dry_temp", "wet_temp", "humidity", "calc_wet_temp",
        "effective_wet_temp", "battery_percent", "battery_voltage", "dcdc_voltage"
    ]

    # Format values, handle None -> "" for CSV
    log_values = []
    for key in data_keys:
        value = data_dict.get(key) # Get value from the passed dictionary
        if key == "timestamp":
            log_values.append(str(value)) # Already string formatted
        elif isinstance(value, (int, float)):
            # Format numbers consistently
            precision = 2 if "temp" in key or "volt" in key else 1 if "hum" in key else 0
            log_values.append(f"{value:.{precision}f}")
        elif value is None:
            log_values.append("") # Empty string for None in CSV
        else:
            log_values.append(str(value)) # Fallback for other types

    try:
        # Create Header if file doesn't exist or is empty
        file_exists = os.path.exists(LOG_FILE)
        is_empty = file_exists and os.path.getsize(LOG_FILE) == 0

        with open(LOG_FILE, 'a', newline='') as f: # Use newline='' for csv handling
             # Write header if needed
             if not file_exists or is_empty:
                 f.write(",".join(header) + "\n")
                 logging.info(f"Schreibe Header in CSV-Logdatei: {LOG_FILE}")

             # Write data row
             f.write(",".join(log_values) + "\n")
        logging.debug(f"Daten erfolgreich in {LOG_FILE} geloggt.")

    except IOError as e:
        logging.error(f"Fehler beim Schreiben in die Logdatei {LOG_FILE}: {e}")
    except Exception as e:
        logging.error(f"Allgemeiner Fehler beim Loggen der Daten: {e}")


def update_sensor_data():
    """Aktualisiert alle Sensorwerte, loggt sie (CSV) und sendet sie via MQTT."""
    global last_readings
    current_readings = {}
    # Use UTC time for consistency across systems and MQTT
    timestamp_dt = datetime.now(timezone.utc)
    timestamp_iso = timestamp_dt.isoformat() # ISO 8601 format, good for MQTT/databases
    timestamp_log_fmt = timestamp_dt.strftime("%Y-%m-%d %H:%M:%S") # Local time format for CSV? Or keep UTC? Let's keep UTC for CSV too.

    dry_temp, wet_temp, humidity, calc_wet_temp, battery_voltage, battery_percent, dcdc_voltage, effective_wet_temp = (None,) * 8

    try:
        with sensor_lock: # Ensure exclusive access to sensors during read cycle
            # Read Temperatures
            dry_temp = read_temp(DRY_SENSOR)
            wet_temp = read_temp(WET_SENSOR)

            # Read Humidity
            humidity = read_humidity()

            # Calculate Wet Bulb Temp (if possible)
            if dry_temp is not None and humidity is not None:
                calc_wet_temp = calculate_wet_bulb(dry_temp, humidity)
            else:
                calc_wet_temp = None # Ensure it's None if inputs are missing

            # Determine effective wet temp (measured preferred, calculated fallback)
            effective_wet_temp = wet_temp if wet_temp is not None else calc_wet_temp

            # Read Voltages and Battery Percentage
            if adc: # Only read if ADC was initialized
                battery_voltage = get_battery_voltage()
                battery_percent = battery_voltage_to_percent(battery_voltage)
                dcdc_voltage = get_dcdc_voltage()
            else:
                battery_voltage, battery_percent, dcdc_voltage = None, None, None


            current_readings = {
                "timestamp": timestamp_iso, # Use ISO format for MQTT/internal state
                "device_id": device_id,
                "dry_temp": dry_temp,
                "wet_temp": wet_temp,       # Measured
                "humidity": humidity,
                "calc_wet_temp": calc_wet_temp, # Calculated
                "effective_wet_temp": effective_wet_temp, # The one used for warnings/logic
                "battery_percent": battery_percent,
                "battery_voltage": battery_voltage,
                "dcdc_voltage": dcdc_voltage,
            }

            # Add system info to the readings payload
            system_info = get_system_info()
            if system_info:
                current_readings.update(system_info) # Merge system info dict

            # ---- Update global state *after* successful reading ----
            last_readings = current_readings.copy()
            # Add a simple last update timestamp for internal checks if needed
            last_readings["last_update_ts"] = time.time()


            # ---- Log locally (CSV) ----
            # Create a dict copy with the local time format for CSV logging if preferred
            # Or just use the UTC ISO format everywhere. Let's stick to UTC ISO.
            csv_log_payload = current_readings.copy()
            csv_log_payload['timestamp'] = timestamp_log_fmt # Use the format for CSV
            log_data(timestamp_log_fmt, csv_log_payload) # Pass the data dict

            # Log summary to system log
            temp_log_str = f"T:{fmt(dry_temp)} NasM:{fmt(wet_temp)} NasB:{fmt(calc_wet_temp)} Eff:{fmt(effective_wet_temp)} H:{fmt(humidity,0)}%"
            bat_log_str = f"Bat:{fmt(battery_percent,0)}%({fmt(battery_voltage,2)}V) DC:{fmt(dcdc_voltage,2)}V"
            sys_log_str = f"CPU:{fmt(system_info.get('cpu_percent'),0)}% RAM:{fmt(system_info.get('memory_percent'),0)}% Disk:{fmt(system_info.get('disk_percent'),0)}%" if system_info else "SysInfo: N/A"
            logging.info(f"Sensoren: {temp_log_str} | {bat_log_str} | {sys_log_str}")

            # --- Sende Daten via MQTT (mit Buffer-Logik) ---
            # Send a clean copy containing only sensor/system data, not internal state like 'last_update_ts'
            mqtt_payload = current_readings.copy()
            if 'last_update_ts' in mqtt_payload: del mqtt_payload['last_update_ts']
            publish_or_buffer_data(mqtt_payload)
            
            return current_readings # Return the full dict including internal state

    except Exception as e:
        logging.error(f"Schwerer Fehler im Sensor-Update-Zyklus: {e}", exc_info=True)
        # Ensure last_readings has some timestamp even on error
        last_readings["timestamp"] = timestamp_iso
        last_readings["last_update_ts"] = time.time()
        return None # Indicate failure

# --- REMOVED format_status_message (Status is now handled by MQTT status topic) ---
# --- REMOVED check_frost_warning (Warning logic should be handled by the MQTT consumer/alerting system) ---
# --- Keeping internal check for changing interval is okay ---
def check_critical_temp_condition(readings):
    """Checks if current temperature necessitates faster polling interval."""
    if not readings:
        return False
    warning_temp = config.get('warning_temp', DEFAULT_CONFIG['warning_temp'])
    effective_wet_temp = readings.get('effective_wet_temp')

    if effective_wet_temp is not None and effective_wet_temp <= warning_temp:
        logging.info(f"Kritische Temperatur erkannt ({effective_wet_temp:.1f}°C <= {warning_temp:.1f}°C). Wechsle zu kürzerem Intervall.")
        return True
    else:
        return False

# --- REMOVED Battery Warning SMS logic --- Battery warnings should be handled by MQTT consumer

def sensor_monitoring_loop():
    """Schleife für die Sensorüberwachung, Datenerfassung und MQTT-Versand."""
    global last_readings # Access global state

    logging.info("Sensorüberwachungsschleife gestartet")

    consecutive_errors = 0
    max_consecutive_errors = 10 # Threshold before logging critical error

    while not shutdown_requested: # Check shutdown flag
        start_time = time.monotonic()
        readings = None
        try:
            # --- 1. Sensorwerte aktualisieren (includes logging, MQTT publish attempt) ---
            readings = update_sensor_data()

            if readings is None:
                # Fehler beim Abrufen der Sensordaten
                consecutive_errors += 1
                logging.warning(f"Fehler beim Abrufen der Sensordaten (#{consecutive_errors}/{max_consecutive_errors})")

                if consecutive_errors >= max_consecutive_errors:
                    logging.critical(f"Maximale Anzahl ({max_consecutive_errors}) aufeinanderfolgender Sensorfehler erreicht!")
                    # Consider triggering a special MQTT status or even a reboot?
                    # For now, just log critically and reset counter to avoid log spam.
                    # Optionally: publish_status(mqtt_client, "offline_sensor_error") ? Needs client access.
                    consecutive_errors = 0 # Reset after critical log

                sleep_time = 60 # Wait shorter after error before retry
            else:
                # Erfolgreich Daten gelesen, Fehlerzähler zurücksetzen
                if consecutive_errors > 0:
                     logging.info(f"Sensor-Lesen nach {consecutive_errors} Fehlern wieder erfolgreich.")
                     consecutive_errors = 0

                # --- 2. Messintervall anpassen ---
                is_critical = check_critical_temp_condition(readings)
                if is_critical:
                    sleep_time = config.get('check_interval_critical', DEFAULT_CONFIG['check_interval_critical'])
                else:
                    sleep_time = config.get('check_interval', DEFAULT_CONFIG['check_interval'])

                # --- 3. Batterie-Check für Energiesparmodus ---
                # (Warning SMS removed, but critical level can still increase interval)
                battery_level = readings.get("battery_percent")
                critical_level = config.get('battery_critical_level', DEFAULT_CONFIG['battery_critical_level'])
                if battery_level is not None and battery_level < critical_level:
                    logging.warning(f"Kritisch niedriger Batteriestand ({battery_level}%) - Energiesparmodus (längeres Intervall)")
                    # Increase sleep time, but not less than critical interval if temps are low
                    sleep_time = max(sleep_time, 1800) # Ensure at least 30 min interval in critical battery state

                logging.info(f"Nächste Messung in {sleep_time} Sekunden.")


            # --- 4. Schlafen bis zur nächsten Messung ---
            # Calculate actual sleep time, accounting for processing time
            elapsed_time = time.monotonic() - start_time
            actual_sleep = max(0, sleep_time - elapsed_time)
            if actual_sleep > 0:
                time.sleep(actual_sleep)

        except Exception as e:
            # Catch errors within the loop itself (outside update_sensor_data)
            consecutive_errors += 1
            logging.error(f"Fehler in der Sensorüberwachungsschleife: {e} (Fehler #{consecutive_errors})", exc_info=True)
            time.sleep(60) # Wait after unexpected loop error

    logging.info("Sensorüberwachungsschleife beendet.")


# --- Buffer functions remain largely the same ---
def load_buffer():
    """Lädt den Puffer für ungesendete Daten aus der Datei"""
    global unsent_data_buffer
    unsent_data_buffer = [] # Start with empty buffer
    if os.path.exists(DATA_BUFFER_FILE):
        try:
            with open(DATA_BUFFER_FILE, 'r') as f:
                # Check if file is empty before trying to load JSON
                if os.path.getsize(DATA_BUFFER_FILE) > 0:
                    unsent_data_buffer = json.load(f)
                    if isinstance(unsent_data_buffer, list):
                         logging.info(f"Ungesendete Daten geladen: {len(unsent_data_buffer)} Einträge")
                    else:
                         logging.warning(f"Datenpufferdatei {DATA_BUFFER_FILE} enthält kein gültiges JSON-Array. Ignoriere Inhalt.")
                         unsent_data_buffer = []
                else:
                     logging.info(f"Datenpufferdatei {DATA_BUFFER_FILE} ist leer.")
        except json.JSONDecodeError as e:
             logging.error(f"Fehler beim Parsen des Datenpuffers {DATA_BUFFER_FILE}: {e}. Starte mit leerem Puffer.")
             unsent_data_buffer = [] # Reset buffer on error
        except Exception as e:
            logging.error(f"Fehler beim Laden des Datenpuffers: {e}")
            unsent_data_buffer = [] # Reset buffer on error
    else:
         logging.info(f"Keine Pufferdatei {DATA_BUFFER_FILE} gefunden. Starte mit leerem Puffer.")


def save_buffer():
    """Speichert den Puffer für ungesendete Daten in die Datei"""
    global unsent_data_buffer # Ensure we use the global list
    try:
        with buffer_lock: # Protect access during save
            max_size = config.get('max_buffer_size', DEFAULT_CONFIG['max_buffer_size'])
            current_size = len(unsent_data_buffer)

            if current_size > max_size:
                # Bei Überlauf die ältesten Einträge verwerfen (FIFO)
                items_to_remove = current_size - max_size
                del unsent_data_buffer[:items_to_remove]
                logging.warning(f"Datenpuffer überläuft ({current_size} > {max_size}). {items_to_remove} älteste Einträge verworfen.")

            # Write the current buffer state to the file
            with open(DATA_BUFFER_FILE, 'w') as f:
                 json.dump(unsent_data_buffer, f) # Save the potentially trimmed list
            logging.debug(f"Datenpuffer gespeichert: {len(unsent_data_buffer)} Einträge")

    except IOError as e:
         logging.error(f"Fehler beim Schreiben der Pufferdatei {DATA_BUFFER_FILE}: {e}")
    except Exception as e:
        logging.error(f"Fehler beim Speichern des Datenpuffers: {e}")

# --- MQTT Callback Functions (Enhanced) ---
def on_connect(client, userdata, flags, rc, properties=None): # Added properties for MQTTv5
    global mqtt_connected
    # --- Lock only for critical state change ---
    connected_successfully = False
    if rc == 0:
        # Acquire lock briefly to update shared state
        with mqtt_lock:
            mqtt_connected = True
        connected_successfully = True # Mark success outside lock
        logging.info(f"Verbunden mit MQTT Broker: {config.get('mqtt_broker')} (Code: {rc})")
    else:
        # Acquire lock briefly to update shared state
        with mqtt_lock:
            mqtt_connected = False
        logging.error(f"MQTT Verbindungsfehler mit Code: {rc}. Prüfe Broker-Adresse, Port, Credentials.")
        # Interpretation einiger häufiger RC-Codes... (keep comments if desired)
        return # Don't proceed if connection failed

    # --- Actions after successful connect (NO LOCK HELD HERE) ---
    if connected_successfully:
        try:
            # Subscribe (safe to do without lock, paho handles internally)
            command_topic = config.get('mqtt_command_topic_template',"").format(device_id=device_id)
            if command_topic:
                qos = config.get('mqtt_qos', DEFAULT_CONFIG['mqtt_qos'])
                client.subscribe(command_topic, qos=qos)
                logging.info(f"Subscribed to command topic: {command_topic} (QoS: {qos})")
            else:
                logging.debug("Kein Command Topic konfiguriert.")

            # Publish initial online status (this function handles its own lock)
            publish_status(client, "online")

            # Try to send buffered data in a new thread
            logging.info("MQTT verbunden, starte Thread zum Senden gepufferter Daten...")
            # Start the thread *after* releasing the lock
            threading.Thread(target=publish_or_buffer_data, args=(None,), name="MQTT_Buffer_Flush").start()

        except Exception as e:
            logging.error(f"Fehler in on_connect nach erfolgreicher Verbindung: {e}", exc_info=True) # Add exc_info

def on_disconnect(client, userdata, rc, properties=None): # Added properties for MQTTv5
    global mqtt_connected
    with mqtt_lock:
        # Only log unexpected disconnects as warnings/errors
        # rc == 0 means a clean disconnect (e.g., by client.disconnect() in graceful_shutdown)
        if rc != 0:
            logging.warning(f"Unerwartete MQTT Verbindungstrennung mit Code: {rc}. LWT sollte gesendet werden. Reconnect wird versucht.")
        else:
            logging.info("MQTT Verbindung sauber getrennt.")
        mqtt_connected = False
    # Automatic reconnection is handled by loop_start/loop_forever

def on_publish(client, userdata, mid):
    # This callback confirms the message has left the client.
    # For QoS 1/2, broker confirmation handling happens internally in Paho.
    logging.debug(f"MQTT Nachricht (MID: {mid}) erfolgreich an Broker übermittelt (lokale Bestätigung).")

def on_message(client, userdata, msg):
    """Callback for receiving MQTT messages (e.g., commands)."""
    topic = msg.topic
    try:
        payload_str = msg.payload.decode('utf-8')
        logging.info(f"MQTT Nachricht empfangen - Topic: {topic}, Payload: {payload_str}")

        # --- Add Command Processing Logic Here ---
        command_topic = config.get('mqtt_command_topic_template',"").format(device_id=device_id)

        if topic == command_topic:
             process_mqtt_command(payload_str)

    except Exception as e:
        logging.error(f"Fehler bei der Verarbeitung der MQTT Nachricht von Topic {topic}: {e}")

def process_mqtt_command(payload_str):
    """Processes commands received via MQTT."""
    global config # Allow modification of config
    try:
        # Example: Expecting JSON like {"command": "reboot"} or {"command": "set_threshold", "value": 1.5}
        command_data = json.loads(payload_str)
        command = command_data.get("command", "").strip().upper()
        value = command_data.get("value") # Might be None

        logging.info(f"Verarbeite MQTT Kommando: {command}, Wert: {value}")

        if command == "REBOOT":
            logging.warning("MQTT Reboot Kommando empfangen. Starte Neustart...")
            # Publish status before rebooting if possible
            publish_status(mqtt_client, "rebooting")
            time.sleep(2) # Give MQTT time
            try:
                # Use subprocess to detach the reboot process
                subprocess.Popen(['sudo', 'reboot'])
                # Call graceful shutdown to clean up before the reboot takes effect
                graceful_shutdown(signal.SIGTERM, None) # Simulate TERM signal
            except Exception as reboot_err:
                logging.error(f"Fehler beim Ausführen des Neustart-Befehls: {reboot_err}")

        elif command == "SET_THRESHOLD":
            try:
                new_threshold = float(value)
                old_threshold = config.get('warning_temp', DEFAULT_CONFIG['warning_temp'])
                config['warning_temp'] = new_threshold
                save_config()
                logging.info(f"Warnschwellwert via MQTT von {old_threshold}°C auf {new_threshold}°C geändert.")
                # Optionally publish confirmation back or update status?
            except (TypeError, ValueError) as e:
                logging.error(f"Ungültiger Wert für SET_THRESHOLD Kommando: {value}. Fehler: {e}")
            except Exception as e:
                logging.error(f"Fehler beim Setzen des Schwellwerts via MQTT: {e}")

        elif command == "GET_STATUS":
             # Force a sensor update and publish
             logging.info("GET_STATUS Kommando empfangen. Führe Sensor-Update aus.")
             update_sensor_data() # This will publish new data

        # Add more commands here (e.g., GET_CONFIG, SET_CONFIG_ITEM, ...)

        else:
            logging.warning(f"Unbekanntes MQTT Kommando empfangen: {command}")

    except json.JSONDecodeError:
        logging.error(f"Konnte MQTT Kommando-Payload nicht als JSON parsen: {payload_str}")
    except Exception as e:
        logging.error(f"Fehler bei der Verarbeitung des MQTT Kommandos: {e}")


# --- Status Publishing Function (NEW) ---
def publish_status(client, status_string):
    """Publishes a device status message (online, offline_*) as a retained JSON payload."""
    if not client or not device_id:
        logging.error("Kann Status nicht publishen: MQTT Client oder Device ID nicht verfügbar.")
        return False

    status_topic = config.get('mqtt_status_topic_template',"").format(device_id=device_id)
    if not status_topic:
        logging.error("Kann Status nicht publishen: Status Topic nicht in Konfiguration gefunden.")
        return False

    # Use UTC time for status timestamp
    timestamp_iso = datetime.now(timezone.utc).isoformat()
    current_ip = None
    try:
        # Get current IP only when publishing 'online', avoid unnecessary calls otherwise
         if status_string == "online":
             current_ip = get_system_info().get("ip_address", "N/A")
    except Exception:
        logging.warning("Konnte IP-Adresse für Status-Update nicht ermitteln.")
        current_ip = "N/A"


    payload = {
        "status": status_string,
        "timestamp_iso": timestamp_iso,
        "device_id": device_id,
        "ip_address": current_ip # Add IP only on online msg or specific statuses
    }
    # Remove None values for cleaner JSON, especially ip_address
    payload = {k: v for k, v in payload.items() if v is not None}

    payload_str = json.dumps(payload)
    qos = config.get('mqtt_qos', DEFAULT_CONFIG['mqtt_qos']) # Use configured QoS

    msg_info = None
    try:
        # --- Lock ONLY around the publish call ---
        with mqtt_lock:
            # Check connection status *inside* lock before publishing might be safer
            # although paho usually queues if temporarily disconnected. Let's trust paho queueing for now.
            if not mqtt_connected and "offline" not in status_string: # Allow publishing offline status even if flag is false
                 logging.warning(f"MQTT nicht verbunden, überspringe Status-Publish '{status_string}' (außer offline).")
                 return False

            msg_info = client.publish(status_topic, payload_str, qos=qos, retain=True)
        # --- Lock released ---

        # Optional: Wait for publish confirmation for important status messages like shutdown
        # Be cautious with waiting in callbacks, prefer short timeouts
        # if "offline" in status_string or "rebooting" in status_string:
        #     try:
        #         msg_info.wait_for_publish(timeout=2.0) # Wait up to 2 seconds
        #     except RuntimeError: # wait_for_publish not available on all results
        #          pass
        #     except ValueError: # Timeout occurred
        #          logging.warning(f"Timeout beim Warten auf Publish-Bestätigung für Status '{status_string}'.")


        if msg_info and msg_info.rc == mqtt.MQTT_ERR_SUCCESS:
            logging.info(f"Status '{status_string}' erfolgreich an MQTT Topic '{status_topic}' gesendet (Retained: True, QoS: {qos}). MID={msg_info.mid}")
            return True
        elif msg_info:
             logging.error(f"Fehler beim Senden des Status '{status_string}' an MQTT Topic '{status_topic}'. RC={msg_info.rc}")
             return False
        else:
             # This case might happen if publish wasn't attempted due to connection check inside lock
             logging.debug(f"Status-Publish '{status_string}' nicht versucht (vermutlich MQTT nicht verbunden).")
             return False


    except Exception as e:
        # Catch errors like broken pipe if connection drops during publish
        logging.error(f"Fehler beim Publishen des Status '{status_string}': {e}", exc_info=True)
        # Consider updating connection status here? Risky within publish itself.
        # with mqtt_lock:
        #      global mqtt_connected
        #      mqtt_connected = False # Assume disconnected on publish error
        return False


# --- MQTT Initialization (Enhanced) ---
def init_mqtt_client():
    global mqtt_client, device_id
    if not device_id:
        logging.critical("Device ID nicht gesetzt, kann MQTT Client nicht initialisieren.")
        return False

    broker = config.get('mqtt_broker')
    port = config.get('mqtt_port')
    if not broker or broker == DEFAULT_CONFIG['mqtt_broker']:
         logging.warning("MQTT Broker nicht konfiguriert oder ist Standardwert. MQTT deaktiviert.")
         return False
    if not port:
         logging.error("MQTT Port nicht konfiguriert.")
         return False


    try:
        with mqtt_lock:
            # Use device_id as client_id for uniqueness and clarity
            # Using MQTTv5 for better features like properties, reason codes
            mqtt_client = mqtt.Client(client_id=device_id, protocol=mqtt.MQTTv5)
            logging.info(f"Initialisiere MQTT Client (ID: {device_id}, Protokoll: MQTTv5)")


            # --- Configure Last Will and Testament (LWT) ---
            status_topic = config.get('mqtt_status_topic_template',"").format(device_id=device_id)
            if status_topic:
                lwt_payload = json.dumps({
                    "status": "offline_unexpected",
                    # Timestamp will be when LWT is SET by client, not when triggered by broker
                    "timestamp_iso": datetime.now(timezone.utc).isoformat(),
                    "device_id": device_id
                })
                qos = config.get('mqtt_qos', DEFAULT_CONFIG['mqtt_qos'])
                mqtt_client.will_set(status_topic, payload=lwt_payload, qos=qos, retain=True)
                logging.info(f"LWT konfiguriert für Topic: '{status_topic}' (Retained: True, QoS: {qos})")
            else:
                 logging.warning("Kein Status Topic Template konfiguriert - LWT nicht gesetzt.")

            # Assign callbacks
            mqtt_client.on_connect = on_connect
            mqtt_client.on_disconnect = on_disconnect
            mqtt_client.on_publish = on_publish
            mqtt_client.on_message = on_message

            # Set username/password if configured
            username = config.get('mqtt_username')
            password = config.get('mqtt_password')
            if username: # Check for username, assume password needed if user is set
                mqtt_client.username_pw_set(username, password)
                logging.info("MQTT Benutzernamen/Passwort gesetzt.")

            # Start the network loop in a background thread
            # Handles reconnections automatically
            mqtt_client.loop_start()
            logging.info("MQTT Netzwerk-Loop gestartet (im Hintergrund).")


            # Attempt initial connection (non-blocking)
            keepalive = config.get('mqtt_keepalive', DEFAULT_CONFIG['mqtt_keepalive'])
            logging.info(f"Versuche Verbindung zu MQTT Broker: {broker}:{port} (Keepalive: {keepalive}s)")
            mqtt_client.connect_async(broker, port, keepalive)

            return True # Indicates initialization started

    except Exception as e:
        logging.critical(f"Kritischer Fehler bei MQTT Initialisierung: {e}", exc_info=True)
        if mqtt_client: # Ensure loop is stopped if connect fails badly
             try: mqtt_client.loop_stop()
             except: pass
        mqtt_client = None # Ensure client is None on failure
        return False

# --- Publish or Buffer Data function (Modified to handle JSON conversion) ---
def publish_or_buffer_data(data_payload):
    """
    Versucht, Daten via MQTT zu publishen. Puffert bei Fehlschlag.
    Verarbeitet auch den Buffer, wenn Verbindung besteht.
    Expects data_payload to be a dictionary.
    """
    global unsent_data_buffer, mqtt_connected # Use global buffer and connection status

    data_to_publish_dicts = []
    with buffer_lock: # Protect buffer operations
        # Add all items from the current buffer
        data_to_publish_dicts.extend(unsent_data_buffer)
        unsent_data_buffer.clear() # Optimistically clear buffer, will re-add failed items

        # Add the new payload if one was provided
        if isinstance(data_payload, dict):
            data_to_publish_dicts.append(data_payload)
        elif data_payload is not None:
            logging.warning(f"Ungültiger Datentyp für publish_or_buffer_data erhalten: {type(data_payload)}. Erwarte Dictionary.")

    if not data_to_publish_dicts:
        logging.debug("Keine neuen oder gepufferten Daten zum Senden via MQTT.")
        return # Nothing to do

    failed_payloads = [] # List to hold dictionaries that failed to send
    successfully_published_count = 0

    # Get MQTT config needed for publishing
    sensor_topic = config.get('mqtt_sensor_topic_template',"").format(device_id=device_id)
    qos = config.get('mqtt_qos', DEFAULT_CONFIG['mqtt_qos'])

    if not sensor_topic:
        logging.error("Sensor Topic nicht konfiguriert. Puffere alle Daten.")
        failed_payloads.extend(data_to_publish_dicts) # Buffer everything
    else:
        # --- Check connection status ONCE before the loop ---
        # Minimal lock duration here
        client_instance = None
        initial_connection_check = False
        with mqtt_lock:
            initial_connection_check = mqtt_connected
            client_instance = mqtt_client # Get instance while locked

        if not initial_connection_check or not client_instance:
            logging.warning("MQTT nicht verbunden (beim Start von publish_or_buffer). Puffere Daten.")
            failed_payloads.extend(data_to_publish_dicts)
        else:
            # Connection seems okay, proceed to loop and publish
            logging.info(f"Versuche {len(data_to_publish_dicts)} Datenpunkte via MQTT zu senden...")
            for payload_dict in data_to_publish_dicts:
                msg_info = None
                publish_error = False
                try:
                    # Ensure payload has device_id (should be added by update_sensor_data)
                    if 'device_id' not in payload_dict:
                        payload_dict['device_id'] = device_id

                    # Convert dict to JSON string
                    payload_str = json.dumps(payload_dict)

                    # --- Lock ONLY around the publish call ---
                    with mqtt_lock:
                        # Check connection *again* just before sending, as it might have dropped
                        if not mqtt_connected:
                            logging.warning("MQTT Verbindung während Publish-Loop verloren. Puffere.")
                            publish_error = True # Mark error to handle outside lock
                        else:
                            # Actual publish call using the client instance obtained earlier
                            msg_info = client_instance.publish(
                                sensor_topic,
                                payload=payload_str,
                                qos=qos,
                                retain=False # Sensor data usually not retained
                            )
                    # --- Lock Released ---

                    # --- Process result outside the lock ---
                    if publish_error:
                        # If connection lost inside lock check, buffer current item and stop loop
                        failed_payloads.append(payload_dict)
                        # Add remaining items back to the buffer list
                        remaining_index = data_to_publish_dicts.index(payload_dict) + 1
                        failed_payloads.extend(data_to_publish_dicts[remaining_index:])
                        logging.warning(f"Publish-Loop unterbrochen, {len(failed_payloads)} Elemente zurück in Buffer.")
                        break # Stop trying for this batch

                    # Check publish result if no error was flagged
                    if msg_info and msg_info.rc == mqtt.MQTT_ERR_SUCCESS:
                        logging.debug(f"Datenpunkt erfolgreich in MQTT Publish-Warteschlange (MID: {msg_info.mid}).")
                        successfully_published_count += 1
                    elif msg_info: # msg_info exists but rc is not success
                        logging.error(f"MQTT Publish Fehler (Code: {msg_info.rc}). Puffere Datenpunkt.")
                        failed_payloads.append(payload_dict)
                        # Consider breaking loop if error suggests persistent issue? Maybe not for QoS 1 retries.
                    else:
                         # This case should ideally not be reached if publish_error was False
                         logging.error("Unerwarteter Zustand nach Publish-Versuch (kein msg_info / publish_error=False). Puffere Datenpunkt.")
                         failed_payloads.append(payload_dict)

                except json.JSONEncodeError as json_err:
                    logging.error(f"Fehler beim Kodieren der Daten zu JSON: {json_err}. Überspringe Datenpunkt: {payload_dict}")
                    # Do not re-buffer data that cannot be encoded
                except Exception as e:
                    logging.error(f"Fehler beim Vorbereiten/Publishen via MQTT: {e}. Puffere Datenpunkt.", exc_info=True)
                    failed_payloads.append(payload_dict) # Buffer on unexpected errors


    # --- Update buffer with any failed payloads outside the publish loop ---
    if failed_payloads:
        with buffer_lock: # Protect buffer access
            # Prepend failed items back to the start of the buffer
            # so they are retried first next time.
            unsent_data_buffer = failed_payloads + unsent_data_buffer
            save_buffer() # Persist buffer immediately after adding failed items

    log_level = logging.INFO if successfully_published_count > 0 or failed_payloads else logging.DEBUG
    logging.log(log_level, f"MQTT Publish Ergebnis: Erfolgreich/In Queue: {successfully_published_count}, Neu gepuffert: {len(failed_payloads)}, Aktueller Buffer: {len(unsent_data_buffer)}")



# --- Graceful Shutdown Handler (NEW) ---
def graceful_shutdown(signum, frame):
    """Handles SIGINT and SIGTERM for clean shutdown."""
    global shutdown_requested
    if shutdown_requested: # Avoid running multiple times if signal received again
        return
    shutdown_requested = True # Set flag to stop main loops

    signame = signal.Signals(signum).name
    logging.warning(f"Signal {signame} ({signum}) empfangen. Fahre System sauber herunter...")
    print(f"\nSignal {signame} empfangen. Fahre herunter...") # Also print to console

    # 1. Stop the Sensor Thread (by checking shutdown_requested flag)
    #    Give it a moment to finish its current cycle if possible.
    #    The main loop check will also stop trying to restart it.

    # 2. Publish the graceful offline status
    if mqtt_client and mqtt_connected:
        logging.info("Sende 'offline_graceful' Status via MQTT...")
        publish_status(mqtt_client, "offline_graceful")
        # Give MQTT a moment to send the message
        time.sleep(1.5) # Increased slightly

    # 3. Stop the MQTT loop
    if mqtt_client:
        logging.info("Stoppe MQTT Netzwerk-Loop...")
        try:
            mqtt_client.loop_stop()
        except Exception as e:
             logging.error(f"Fehler beim Stoppen des MQTT Loops: {e}")

    # 4. Disconnect MQTT client cleanly
    if mqtt_client:
        logging.info("Trenne MQTT Verbindung...")
        try:
            mqtt_client.disconnect()
        except Exception as e:
             logging.error(f"Fehler beim Trennen der MQTT Verbindung: {e}")

    # 5. Save the data buffer one last time
    logging.info("Speichere Datenpuffer...")
    save_buffer()

    # 6. Clean up GPIO
    logging.info("Räume GPIO auf...")
    try:
         GPIO.cleanup()
    except Exception as e:
         logging.error(f"Fehler beim GPIO Cleanup: {e}")


    logging.warning("System heruntergefahren.")
    print("System heruntergefahren.")
    sys.exit(0) # Exit cleanly


def main():
    """Hauptfunktion"""
    global device_id, mqtt_client # Allow modification

    # --- Register Signal Handlers ---
    try:
        signal.signal(signal.SIGINT, graceful_shutdown)  # Handle Ctrl+C
        signal.signal(signal.SIGTERM, graceful_shutdown) # Handle kill/system shutdown
        logging.info("Signal Handler für SIGINT und SIGTERM registriert.")
    except Exception as e:
         logging.error(f"Fehler beim Registrieren der Signal Handler: {e}")
         # Continue execution, but shutdown might not be graceful

    try:
        # --- Initialisierung ---
        print("=" * 50)
        print("Integriertes Frostwarnsystem mit MQTT")
        print("=" * 50)
        logging.info("============================================")
        logging.info("   Frostwarnsystem mit MQTT wird gestartet  ")
        logging.info("============================================")


        # Konfiguration laden (sets global device_id)
        load_config()
        print(f"Device ID: {device_id}")


        # Buffer laden
        load_buffer()

        # --- Hardware Initialisierung ---
        logging.info("Initialisiere Hardware...")

        # DS18B20 (already done globally, just log status)
        if DRY_SENSOR: logging.info(f"Trockentemperatursensor initialisiert: {DRY_SENSOR}")
        else: logging.warning("Kein Trockentemperatursensor gefunden/initialisiert!")
        if WET_SENSOR: logging.info(f"Nasstemperatursensor initialisiert: {WET_SENSOR}")
        else: logging.warning("Kein Nasstemperatursensor gefunden/initialisiert!")

        # DHT22
        if init_dht_sensor(): logging.info("DHT22 Sensor initialisiert.")
        else: logging.warning("DHT22 Sensor nicht initialisiert oder nicht verfügbar.")

        # ADS1115 & Kalibrierung prüfen
        battery_monitor_available = init_battery_monitor()
        if battery_monitor_available:
            logging.info("Batterie-/Spannungs-Monitoring (ADS1115) aktiv.")
            # Check if calibration has been done (by checking if factor is default 1.0)
            # Allow calibration via command line arguments for setup purposes
            if len(sys.argv) > 1 and sys.argv[1].upper() == 'CALIBRATE':
                 print("\n--- START KALIBRIERUNGSMODUS ---")
                 print("Wähle aus:")
                 print(" 1: Batteriespannung kalibrieren")
                 print(" 2: DC-DC Ausgangsspannung kalibrieren")
                 print(" 3: Beide kalibrieren")
                 choice = input("Auswahl (1-3): ")
                 if choice == '1' or choice == '3':
                      calibrate_battery_sensor()
                 if choice == '2' or choice == '3':
                      calibrate_dcdc_sensor()
                 print("--- ENDE KALIBRIERUNGSMODUS ---")
                 print("Skript wird jetzt beendet. Bitte ohne 'calibrate' Argument neu starten.")
                 sys.exit(0) # Exit after calibration mode
            else:
                 # Check if factors seem uncalibrated (still 1.0) and log warning
                 if config.get('battery_calibration_factor', 1.0) == 1.0:
                      logging.warning("Batteriespannungs-Kalibrierungsfaktor ist 1.0. Ggf. Kalibrierung durchführen (Skript mit 'calibrate' starten).")
                 if config.get('dcdc_calibration_factor', 1.0) == 1.0:
                      logging.warning("DC-DC Spannungs-Kalibrierungsfaktor ist 1.0. Ggf. Kalibrierung durchführen.")
        else:
            logging.warning("Batterie-/Spannungs-Monitoring (ADS1115) nicht verfügbar.")


        # --- MQTT Initialisierung ---
        if not init_mqtt_client():
            logging.warning("MQTT Client konnte nicht initialisiert werden oder ist nicht konfiguriert. Betrieb ohne MQTT-Verbindung.")
            print("WARNUNG: MQTT Client nicht initialisiert/konfiguriert.")
            # Continue without MQTT, data will be buffered.
        else:
            # Allow some time for initial connection attempt before starting sensor loop
            logging.info("Warte kurz auf initiale MQTT Verbindung...")
            time.sleep(5) # Wait 5 seconds


        # --- Initialer Systemstatus & Sensor Read ---
        logging.info("Führe erste Sensor-Messung durch...")
        update_sensor_data() # This now includes logging and MQTT publish attempt


        # --- Threads starten ---
        # Only Sensor Thread is needed now
        sensor_thread = threading.Thread(target=sensor_monitoring_loop, name="SensorMonitor", daemon=True)
        sensor_thread.start()

        logging.info("Sensor-Thread gestartet, System läuft.")
        print("System läuft... (Drücke Strg+C zum Beenden)")

        # --- Hauptschleife (Überwachung der Threads & MQTT Status) ---
        last_status_publish_time = 0
        while not shutdown_requested:
            # 1. Prüfen, ob Sensor-Thread noch läuft
            if not sensor_thread.is_alive():
                if not shutdown_requested: # Don't try restart if shutdown is in progress
                     logging.error("Sensor-Thread ist unerwartet gestorben! Versuche Neustart...")
                     # Optional: Publish error status?
                     # publish_status(mqtt_client, "offline_thread_error")
                     sensor_thread = threading.Thread(target=sensor_monitoring_loop, name="SensorMonitor", daemon=True)
                     sensor_thread.start()
                     time.sleep(5) # Wait a bit after restart
                else:
                     logging.info("Sensor-Thread beendet (Shutdown angefordert).")


            # 2. Prüfen, ob MQTT verbunden ist und Buffer senden
            with mqtt_lock:
                 mqtt_is_connected_now = mqtt_connected
            if mqtt_is_connected_now:
                 # Periodically send buffer content if connection is up
                 with buffer_lock:
                      buffer_has_items = len(unsent_data_buffer) > 0
                 if buffer_has_items:
                      logging.info(f"MQTT verbunden und Buffer hat {len(unsent_data_buffer)} Einträge. Versuche erneut zu senden...")
                      # Run in thread to avoid blocking main loop for long buffer sends
                      threading.Thread(target=publish_or_buffer_data, args=(None,), name="MQTT_Buffer_Retry").start()


                 # 3. Periodically publish "online" status as a heartbeat
                 current_time = time.time()
                 heartbeat_interval = config.get('mqtt_status_heartbeat_interval', DEFAULT_CONFIG['mqtt_status_heartbeat_interval'])
                 if current_time - last_status_publish_time > heartbeat_interval:
                      if publish_status(mqtt_client, "online"):
                           last_status_publish_time = current_time
                      else:
                           # If publishing status fails, connection might be broken
                           logging.warning("Fehler beim Senden des 'online' Heartbeats. Verbindungsproblem?")
                           # Reduce interval slightly to retry sooner? Or rely on Paho's keepalive?
                           # Let Paho handle keepalive/reconnect for now.

            # 4. Sleep for a while
            time.sleep(30) # Check threads/buffer/heartbeat every 30 seconds

    except KeyboardInterrupt:
         # This should be caught by the signal handler, but as a fallback:
         logging.warning("KeyboardInterrupt im Hauptprogramm abgefangen.")
         graceful_shutdown(signal.SIGINT, None) # Trigger manual shutdown

    except Exception as e:
        logging.critical(f"Kritischer, nicht abgefangener Fehler im Hauptprogramm: {e}", exc_info=True)
        # Attempt to publish an error status
        try:
            if mqtt_client:
                 # Use a blocking publish attempt for critical error
                 publish_status(mqtt_client, "offline_critical_error")
                 time.sleep(2)
        except Exception as final_pub_err:
             logging.error(f"Fehler beim Senden des kritischen Fehlerstatus: {final_pub_err}")

        # Optional: Trigger automatic reboot on critical failure
        # logging.critical("Löse automatischen Neustart nach kritischem Fehler aus...")
        # try:
        #     subprocess.Popen(['sudo', 'reboot'])
        # except Exception as reboot_err:
        #     logging.error(f"Fehler beim Ausführen des Neustart-Befehls nach kritischem Fehler: {reboot_err}")
        # graceful_shutdown(signal.SIGTERM, None) # Clean up as much as possible

    finally:
        # This block executes even if sys.exit() is called in graceful_shutdown
        # Ensure GPIO cleanup happens if shutdown handler wasn't fully executed
        if not shutdown_requested: # If shutdown wasn't initiated gracefully
             logging.warning("Finally-Block erreicht ohne vorheriges graceful_shutdown. Notfall-Cleanup.")
             if mqtt_client:
                  try: mqtt_client.loop_stop(force=True)
                  except: pass
                  try: mqtt_client.disconnect()
                  except: pass
             try: GPIO.cleanup()
             except: pass
        logging.info("Programm final beendet.")


if __name__ == "__main__":
    main()