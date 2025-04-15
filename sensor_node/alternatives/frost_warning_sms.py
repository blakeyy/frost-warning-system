#!/usr/bin/env python3
"""
Integriertes Frostwarnsystem mit SMS-Service

Dieses Skript kombiniert die Funktionalität des Frostwarnsystems und des SMS-Service.
Es überwacht kontinuierlich die Temperaturen, sendet Warnungen bei kritischen Werten
und bietet eine SMS-basierte Fernsteuerung des Systems.

Funktionen:
- Temperaturüberwachung (Trocken- und Nasstemperatur)
- Automatische Frostwarnungen per SMS
- Statusabfrage per SMS
- Fernkonfiguration von Schwellwerten
- System-Fernsteuerung
- Datenaufzeichnung
"""

import time
import os
import glob
import serial
import RPi.GPIO as GPIO
import adafruit_dht
import board
import busio
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn
from datetime import datetime
import subprocess
import math
import threading
import json
import psutil
import logging

# Logging einrichten
logging.basicConfig(
    filename='/home/pi/frost_system_sms.log',
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Konfiguration
CONFIG_FILE = "/home/pi/frost_config_sms.json"
LOG_FILE = "/home/pi/temp_log.csv"
DEFAULT_CONFIG = {
    "authorized_numbers": ["+49123456789"],  # Autorisierte Nummern
    "warning_temp": 0.0,                    # Warnschwellwert in °C
    "check_interval": 900,                  # 15 Minuten
    "check_interval_critical": 300,         # 5 Minuten
    "sms_check_interval": 60,               # SMS-Prüfintervall in Sekunden
    "status_code": "STATUS",                # SMS-Code für Statusabfrage
    "threshold_code": "THRESHOLD:",         # SMS-Code für Schwellwertänderung
    "reboot_code": "REBOOT",                # SMS-Code für Systemneustart
    "add_number_code": "ADD:",              # SMS-Code für neue autorisierte Nummer
    "remove_number_code": "REMOVE:",        # SMS-Code für Entfernen einer Nummer
    "help_code": "HELP",                    # SMS-Code für Hilfe
    "battery_warning_level": 20,            # Batteriewarnung in Prozent
    "battery_critical_level": 10,           # Kritischer Batteriestand in Prozent
    "battery_calibration_factor": 1.0,      # Kalibrierungsfaktor für Batteriespannung
    "battery_r1": 82000,                    # R1 Widerstand im Spannungsteiler (Ohm)
    "battery_r2": 10000,                    # R2 Widerstand im Spannungsteiler (Ohm)
    "dcdc_calibration_factor": 1.0,         # Kalibrierungsfaktor für DC-DC Ausgangsspannung
    "dcdc_r1": 10000,                       # R1 Widerstand im Spannungsteiler für DC-DC (Ohm)
    "dcdc_r2": 10000                        # R2 Widerstand im Spannungsteiler für DC-DC (Ohm)
}

# Globale Variablen
config = {}
last_readings = {
    "dry_temp": None,
    "wet_temp": None,
    "humidity": None,
    "calc_wet_temp": None,
    "battery": None,
    "battery_voltage": None,
    "dcdc_voltage": None,
    "last_update": None
}

# Warnungsstatus
last_warning_time = 0
warning_sent = False

# Thread-Synchronisierung
sensor_lock = threading.Lock()
gsm_lock = threading.Lock()

# Batterie-Monitoring Variablen
adc = None
battery_channel = None
dcdc_channel = None

# GPIO initialisieren
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

# RESET-Pin für SIM800L
RESET_PIN = 21  # GPIO 21 für SIM800L Reset
GPIO.setup(RESET_PIN, GPIO.OUT)
GPIO.output(RESET_PIN, GPIO.HIGH)  # Setze den Reset-Pin auf HIGH bei Start

# 1-Wire für DS18B20 initialisieren
os.system('modprobe w1-gpio')
os.system('modprobe w1-therm')
base_dir = '/sys/bus/w1/devices/'
# Finde alle DS18B20 Sensoren
device_folders = glob.glob(base_dir + '28*')
device_files = [folder + '/w1_slave' for folder in device_folders]

# Zuweisung der Sensoren
# Erster Sensor: Trockentemperatur, Zweiter: Nasstemperatur (mit Feuchtsocke)
DRY_SENSOR = device_files[0] if len(device_files) > 0 else None
WET_SENSOR = device_files[1] if len(device_files) > 1 else None

# DHT22 für Luftfeuchtigkeit
# try:
#     DHT_SENSOR = adafruit_dht.DHT22(board.D17)
# except Exception as e:
#     DHT_SENSOR = None
#     logging.warning(f"DHT22 Sensor konnte nicht initialisiert werden: {e}")

# GSM-Modul initialisieren
try:
    gsm = serial.Serial('/dev/ttyS0', 9600, timeout=1)
except Exception as e:
    gsm = None
    logging.error(f"GSM-Modul konnte nicht initialisiert werden: {e}")

# Funktion zum Reset des SIM800L-Moduls
def reset_sim800l():
    """
    Setzt das SIM800L-Modul durch einen Hardware-Reset zurück
    """
    logging.info("Führe SIM800L Hardware-Reset durch...")
    try:
        # Pull the reset pin LOW
        GPIO.output(RESET_PIN, GPIO.LOW)
        # Wait for a moment
        time.sleep(1)
        # Pull the reset pin HIGH
        GPIO.output(RESET_PIN, GPIO.HIGH)
        # Wait for the module to initialize
        time.sleep(5)
        logging.info("SIM800L wurde zurückgesetzt")
        return True
    except Exception as e:
        logging.error(f"Fehler beim Zurücksetzen des SIM800L-Moduls: {e}")
        return False

def init_dht_sensor():
    """Initialize the DHT22 sensor with improved configuration"""
    global DHT_SENSOR
    
    try:
        # Import the more reliable CircuitPython library if available
        try:
            import adafruit_dht
            import board
            # Configure GPIO pin with pull-up
            GPIO.setup(17, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            time.sleep(0.1)
            DHT_SENSOR = adafruit_dht.DHT22(board.D17)
            logging.info("DHT22 initialized with CircuitPython library")
        except ImportError:
            logging.warning("CircuitPython DHT library not available, falling back to standard library")
            import Adafruit_DHT
            DHT_SENSOR = Adafruit_DHT
            logging.info("DHT22 initialized with standard library")
            
        return True
    except Exception as e:
        logging.error(f"DHT22 initialization error: {e}")
        DHT_SENSOR = None
        return False

def reset_dht_sensor():
    """Reset the DHT22 sensor GPIO pin when it fails repeatedly"""
    try:
        pin = 17  # DHT22 pin
        # Set pin as output and cycle it
        GPIO.setup(pin, GPIO.OUT)
        GPIO.output(pin, GPIO.LOW)
        time.sleep(0.5)
        GPIO.output(pin, GPIO.HIGH)
        time.sleep(0.5)
        
        # Return to input mode with pull-up
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        time.sleep(1)
        
        # Re-initialize the sensor
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
        
        # Erstelle ADS-Objekt
        adc = ADS.ADS1115(i2c, address=0x48)
        
        # Setze Gain auf 1 für einen Messbereich von ±4.096V
        adc.gain = 1
        
        # Single-ended Eingang an Kanal 0 für Batterie
        battery_channel = AnalogIn(adc, ADS.P0)
        
        # Single-ended Eingang an Kanal 1 für DC-DC Ausgang
        dcdc_channel = AnalogIn(adc, ADS.P1)
        
        logging.info("ADS1115 für Spannungsmessung initialisiert")
        return True
    except Exception as e:
        logging.error(f"Fehler bei der Initialisierung des ADS1115: {e}")
        return False

# Hilfsfunktionen

def load_config():
    """Lädt die Konfiguration aus der Datei oder erstellt Standardwerte"""
    global config
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
                logging.info("Konfiguration geladen")
        else:
            config = DEFAULT_CONFIG
            save_config()
            logging.info("Standardkonfiguration erstellt")
    except Exception as e:
        logging.error(f"Fehler beim Laden der Konfiguration: {e}")
        config = DEFAULT_CONFIG

def save_config():
    """Speichert die aktuelle Konfiguration in die Datei"""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=4)
        logging.info("Konfiguration gespeichert")
    except Exception as e:
        logging.error(f"Fehler beim Speichern der Konfiguration: {e}")

def send_command(cmd, wait_time=1):
    """Sendet einen AT-Befehl an das GSM-Modul und gibt die Antwort zurück"""
    if not gsm:
        logging.error("GSM-Modul nicht verfügbar")
        return ""
    
    with gsm_lock:  # Thread-sicherer Zugriff
        try:
            gsm.write((cmd + '\r\n').encode())
            time.sleep(wait_time)
            response = ''
            while gsm.in_waiting:
                # 'replace' ersetzt ungültige Zeichen durch '?'
                response += gsm.read(gsm.in_waiting).decode('utf-8', 'replace')
            return response
        except Exception as e:
            logging.error(f"Fehler beim Senden von AT-Befehl: {e}")
            return ""

def replace_special_chars(text):
    """Ersetzt deutsche Umlaute und Sonderzeichen für bessere SMS-Kompatibilität"""
    replacements = {
        'ä': 'ae', 'ö': 'oe', 'ü': 'ue',
        'Ä': 'Ae', 'Ö': 'Oe', 'Ü': 'Ue',
        'ß': 'ss', '°': ' Grad'
    }
    
    for char, replacement in replacements.items():
        text = text.replace(char, replacement)
    
    return text

def send_sms(number, message):
    """Sendet eine SMS an die angegebene Nummer mit Ersetzung von Sonderzeichen"""
    if not gsm:
        logging.error("GSM-Modul nicht verfuegbar - kann keine SMS senden")
        return False
    
    try:
        # Sonderzeichen ersetzen
        clean_message = replace_special_chars(message)
        
        logging.info(f"Sende SMS an {number}: {clean_message}")
        send_command('AT')
        send_command('AT+CMGF=1')  # Textmodus
        send_command(f'AT+CMGS="{number}"')
        send_command(clean_message + chr(26))  # Nachricht + CTRL+Z
        time.sleep(5)  # Warten auf Versand
        return True
    except Exception as e:
        logging.error(f"Fehler beim Senden der SMS: {e}")
        return False

def read_sms():
    """
    Liest alle ungelesenen SMS vom GSM-Modul und gibt sie als Liste zurück
    Jede SMS ist ein Dictionary mit 'number', 'date', 'time' und 'message'
    """
    if not gsm:
        logging.error("GSM-Modul nicht verfügbar - kann keine SMS lesen")
        return []
    
    try:
        # SMS-Textmodus aktivieren
        send_command('AT+CMGF=1')
        
        # Alle SMS lesen
        response = send_command('AT+CMGL="ALL"', wait_time=3)
        
        # Keine SMS vorhanden
        if "+CMGL:" not in response:
            return []
        
        # SMS-Nachrichten parsen
        sms_list = []
        lines = response.split('\n')
        current_sms = None
        
        for line in lines:
            if line.startswith('+CMGL:'):
                # Neue SMS gefunden
                parts = line.split(',')
                index = parts[0].split(':')[1].strip()
                number = parts[2].replace('"', '').strip()
                date_time = parts[4].replace('"', '').strip() + ' ' + parts[5].replace('"', '').strip()
                
                current_sms = {
                    'index': index,
                    'number': number,
                    'datetime': date_time,
                    'message': ''
                }
            elif current_sms is not None:
                # SMS-Inhalt
                if line.strip() and not line.startswith('OK'):
                    current_sms['message'] = line.strip()
                    sms_list.append(current_sms)
                    current_sms = None
        
        # Gelesene SMS löschen
        if sms_list:
            send_command('AT+CMGD=1,4')  # Alle SMS löschen
            logging.info(f"Gelesen und gelöscht: {len(sms_list)} SMS")
            
        return sms_list
        
    except Exception as e:
        logging.error(f"Fehler beim Lesen der SMS: {e}")
        return []

def read_temp_raw(device_file):
    """Liest die Rohdaten vom Temperatursensor"""
    try:
        with open(device_file, 'r') as f:
            lines = f.readlines()
        return lines
    except Exception as e:
        logging.error(f"Fehler beim Lesen des Temperatursensors: {e}")
        return None

def read_temp(device_file):
    """Liest die Temperatur vom DS18B20 Sensor"""
    if not device_file:
        return None
    
    try:
        lines = read_temp_raw(device_file)
        if not lines:
            return None
        
        while lines[0].strip()[-3:] != 'YES':
            time.sleep(0.2)
            lines = read_temp_raw(device_file)
            if not lines:
                return None
        
        equals_pos = lines[1].find('t=')
        if equals_pos != -1:
            temp_string = lines[1][equals_pos+2:]
            temp_c = float(temp_string) / 1000.0
            return temp_c
        return None
    except Exception as e:
        logging.error(f"Fehler beim Lesen der Temperatur: {e}")
        return None

def read_humidity():
    """Improved humidity reading function with auto-reset capability"""
    global DHT_SENSOR
    
    if not DHT_SENSOR:
        if not init_dht_sensor():
            return None
    
    max_retries = 5  # Increased from 3
    for retry in range(max_retries):
        try:
            # Check which DHT library we're using
            if hasattr(DHT_SENSOR, 'humidity'):
                # CircuitPython library
                humidity = DHT_SENSOR.humidity
                if humidity is not None:
                    return humidity
            else:
                # Standard Adafruit library
                humidity, _ = Adafruit_DHT.read_retry(Adafruit_DHT.DHT22, 17)
                if humidity is not None:
                    return humidity
            
            logging.debug(f"DHT22 returned None, retry {retry+1}/{max_retries}")
            time.sleep(2)
        except Exception as e:
            logging.debug(f"DHT22 read error: {e}, retry {retry+1}/{max_retries}")
            
            # After 3 failed attempts, try resetting the sensor
            if retry == 2:
                logging.warning("DHT22 not responding, attempting sensor reset")
                reset_dht_sensor()
            
            time.sleep(2)
    
    logging.error(f"DHT22 read failed after {max_retries} attempts")
    return None

def calculate_wet_bulb(temp, humidity):
    """Berechnet die Nasstemperatur aus Trockentemperatur und Luftfeuchtigkeit"""
    if temp is None or humidity is None:
        return None
    
    try:
        # Vereinfachte Formel nach Stull (2011)
        c1 = 0.151977
        c2 = 8.313659
        c3 = 1.676331
        c4 = 0.00391838
        c5 = 0.023101
        c6 = 4.686035
        
        tw = temp * math.atan(c1 * math.sqrt(humidity + c2)) + math.atan(temp + humidity) - math.atan(humidity - c3) + c4 * (humidity**1.5) * math.atan(c5 * humidity) - c6
        return tw
    except Exception as e:
        logging.error(f"Fehler bei der Nasstemperaturberechnung: {e}")
        return None

def get_stable_voltage(channel, samples=10, delay=0.05):
    """Liefert einen stabileren Spannungswert durch Mittelwertbildung"""
    if channel is None:
        return None
    
    try:
        readings = []
        for _ in range(samples):
            readings.append(channel.voltage)
            time.sleep(delay)
        
        # Entferne Ausreißer, wenn genügend Samples vorhanden
        if len(readings) > 4:
            readings.sort()
            readings = readings[1:-1]  # Entferne höchsten und niedrigsten Wert
        
        # Berechne Durchschnitt
        return sum(readings) / len(readings)
    except Exception as e:
        logging.error(f"Fehler bei der Spannungsmessung: {e}")
        return None

def get_battery_voltage():
    """Berechne kalibrierte Batteriespannung"""
    voltage_at_pin = get_stable_voltage(battery_channel)
    
    if voltage_at_pin is None:
        return None
    
    # Berechne Spannungsteiler-Verhältnis
    r1 = config.get('battery_r1', 82000)  # 82kΩ
    r2 = config.get('battery_r2', 10000)  # 10kΩ
    divider_ratio = (r1 + r2) / r2
    
    # Wende Kalibrierungsfaktor an
    calibration_factor = config.get('battery_calibration_factor', 1.0)
    
    return voltage_at_pin * divider_ratio * calibration_factor

def get_dcdc_voltage():
    """Berechne kalibrierte DC-DC Ausgangsspannung"""
    voltage_at_pin = get_stable_voltage(dcdc_channel)
    
    if voltage_at_pin is None:
        return None
    
    # Berechne Spannungsteiler-Verhältnis
    r1 = config.get('dcdc_r1', 10000)  # 10kΩ
    r2 = config.get('dcdc_r2', 10000)  # 10kΩ
    divider_ratio = (r1 + r2) / r2
    
    # Wende Kalibrierungsfaktor an
    calibration_factor = config.get('dcdc_calibration_factor', 1.0)
    
    return voltage_at_pin * divider_ratio * calibration_factor

def battery_voltage_to_percent(voltage):
    """Konvertiere Batteriespannung in Prozentwert (Beispielimplementierung)"""
    if voltage is None:
        return None
    
    # Beispiel für eine 12V Bleibatterie
    # Anpassung entsprechend deiner Batterieart nötig
    max_voltage = 14.4  # Vollständig geladen
    min_voltage = 11.0  # Entladen
    
    # Lineare Umrechnung
    if voltage >= max_voltage:
        return 100
    elif voltage <= min_voltage:
        return 0
    else:
        return int(((voltage - min_voltage) / (max_voltage - min_voltage)) * 100)

def calibrate_voltage_sensor(channel, r1, r2, name, config_key):
    """Kalibriere einen Spannungsmesser mit einem bekannten Wert"""
    global config
    
    try:
        # Sammle Messwerte für Kalibrierung
        voltage_at_pin = get_stable_voltage(channel, samples=20, delay=0.1)
        if voltage_at_pin is None:
            logging.error(f"Konnte keine Spannung am Pin für {name} messen")
            return False
        
        # Berechne unkalibrierte Spannung
        divider_ratio = (r1 + r2) / r2
        raw_voltage = voltage_at_pin * divider_ratio
        
        # Frage nach der tatsächlichen Spannung
        print(f"\nGemessene Spannung am ADC für {name}: {voltage_at_pin:.4f}V")
        print(f"Berechnete {name} (unkalibriert): {raw_voltage:.2f}V")
        
        actual_voltage = float(input(f"Gib die mit dem Multimeter gemessene {name} ein: "))
        
        if actual_voltage <= 0:
            print("Ungültige Spannung")
            return False
        
        # Berechne Kalibrierungsfaktor
        calibration_factor = actual_voltage / raw_voltage
        
        # Speichere in Konfiguration
        config[config_key] = calibration_factor
        save_config()
        
        print(f"Kalibrierungsfaktor für {name} gespeichert: {calibration_factor:.4f}")
        return True
        
    except Exception as e:
        logging.error(f"Fehler bei der Kalibrierung von {name}: {e}")
        return False

def calibrate_battery_sensor():
    """Kalibriere den Batteriespannungsmesser"""
    r1 = config.get('battery_r1', 82000)
    r2 = config.get('battery_r2', 10000)
    return calibrate_voltage_sensor(
        battery_channel, r1, r2, "Batteriespannung", "battery_calibration_factor"
    )

def calibrate_dcdc_sensor():
    """Kalibriere den DC-DC Ausgangsspannungsmesser"""
    r1 = config.get('dcdc_r1', 10000)
    r2 = config.get('dcdc_r2', 10000)
    return calibrate_voltage_sensor(
        dcdc_channel, r1, r2, "DC-DC Ausgangsspannung", "dcdc_calibration_factor"
    )

def check_battery():
    """Batteriestand überprüfen und als Prozentwert zurückgeben"""
    try:
        # Batteriespannung messen
        voltage = get_battery_voltage()
        
        if voltage is None:
            return None
        
        # Aktualisiere die letzte gemessene Spannung
        last_readings["battery_voltage"] = voltage
        
        # Konvertiere in Prozentwert
        percent = battery_voltage_to_percent(voltage)
        
        logging.debug(f"Batteriespannung: {voltage:.2f}V, Ladezustand: {percent}%")
        
        return percent
    except Exception as e:
        logging.error(f"Fehler beim Lesen des Batteriestatus: {e}")
        return None

def get_system_info():
    """Sammelt Systeminformationen wie CPU-Last, Speicherverbrauch, etc."""
    try:
        # CPU-Last
        cpu_percent = psutil.cpu_percent()
        
        # Speicherverbrauch
        memory = psutil.virtual_memory()
        memory_percent = memory.percent
        
        # Speicherplatz
        disk = psutil.disk_usage('/')
        disk_percent = disk.percent
        
        # Systemlaufzeit
        uptime_seconds = time.time() - psutil.boot_time()
        uptime_days = int(uptime_seconds // (24 * 3600))
        uptime_hours = int((uptime_seconds % (24 * 3600)) // 3600)
        uptime_minutes = int((uptime_seconds % 3600) // 60)
        uptime_str = f"{uptime_days}d {uptime_hours}h {uptime_minutes}m"
        
        return {
            "cpu": cpu_percent,
            "memory": memory_percent,
            "disk": disk_percent,
            "uptime": uptime_str
        }
    except Exception as e:
        logging.error(f"Fehler beim Sammeln der Systeminformationen: {e}")
        return None

def log_data(dry_temp, wet_temp, humidity, calculated_wet_temp):
    """Daten in CSV-Datei loggen"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Batteriespannung und Prozentwert hinzufügen
    battery_percent = last_readings.get('battery')
    battery_voltage = last_readings.get('battery_voltage')
    dcdc_voltage = last_readings.get('dcdc_voltage')
    
    # Erstelle Header, wenn Datei nicht existiert
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'w') as f:
            f.write("Zeitstempel,Trockentemperatur,Nasstemperatur_gemessen,Luftfeuchtigkeit,"
                    "Nasstemperatur_berechnet,Batterie_Prozent,Batterie_Spannung,DCDC_Spannung\n")
    
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(f"{timestamp},{dry_temp},{wet_temp},{humidity},{calculated_wet_temp},"
                   f"{battery_percent},{battery_voltage},{dcdc_voltage}\n")
    except Exception as e:
        logging.error(f"Fehler beim Loggen der Daten: {e}")

def update_sensor_data():
    """Aktualisiert alle Sensorwerte"""
    global last_readings
    
    try:
        with sensor_lock:
            # Temperaturen auslesen
            dry_temp = read_temp(DRY_SENSOR)
            wet_temp = read_temp(WET_SENSOR)
            humidity = read_humidity()
            
            # Nasstemperatur berechnen (falls Sensor nicht verfügbar)
            calc_wet_temp = None
            if dry_temp is not None and humidity is not None:
                calc_wet_temp = calculate_wet_bulb(dry_temp, humidity)
            
            # Batteriestatus
            battery_voltage = get_battery_voltage()
            battery = battery_voltage_to_percent(battery_voltage) if battery_voltage is not None else None
            
            # DC-DC Ausgangsspannung
            dcdc_voltage = get_dcdc_voltage()
            
            # Daten speichern
            last_readings = {
                "dry_temp": dry_temp,
                "wet_temp": wet_temp,
                "humidity": humidity,
                "calc_wet_temp": calc_wet_temp,
                "battery": battery,
                "battery_voltage": battery_voltage,
                "dcdc_voltage": dcdc_voltage,
                "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }

            # Daten loggen
            log_data(dry_temp, wet_temp, humidity, calc_wet_temp)
            
            # Ausgabe für Debugging
            battery_info = f", Batterie: {battery}% ({battery_voltage:.2f}V)" if battery is not None else ""
            dcdc_info = f", DC-DC: {dcdc_voltage:.2f}V" if dcdc_voltage is not None else ""
            logging.info(f"Sensordaten aktualisiert - Trockentemp: {dry_temp} °C, " 
                         f"Nasstemp: {wet_temp} °C, Luftfeuchtigkeit: {humidity} %{battery_info}{dcdc_info}")
            
            return last_readings
            
    except Exception as e:
        logging.error(f"Fehler beim Aktualisieren der Sensordaten: {e}")
        return None

def format_status_message():
    """Erstellt eine formatierte Statusnachricht mit allen aktuellen Sensorwerten"""
    try:
        # Aktuelle Zeit hinzufügen
        current_time = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

        # Sensordaten aktualisieren
        readings = update_sensor_data()
        
        # Systeminformationen abrufen
        sys_info = get_system_info()
        
        # Nasstemperatur bestimmen (gemessen oder berechnet)
        actual_wet_temp = readings["wet_temp"]
        if actual_wet_temp is None:
            actual_wet_temp = readings["calc_wet_temp"]
        
        # Nachricht zusammenstellen
        message = f"Frostwarnsystem Status ({current_time}):\n"
        
        # Temperaturdaten
        message += f"Trockentemp: {readings['dry_temp']:.1f} Grad\n" if readings['dry_temp'] is not None else "Trockentemp: N/A\n"
        message += f"Nasstemp (gemessen): {readings['wet_temp']:.1f} Grad\n" if readings['wet_temp'] is not None else "Nasstemp (gemessen): N/A\n"
        message += f"Nasstemp (berechnet): {readings['calc_wet_temp']:.1f} Grad\n" if readings['calc_wet_temp'] is not None else "Nasstemp (berechnet): N/A\n"
        message += f"Luftfeuchte: {readings['humidity']:.1f}%\n" if readings['humidity'] is not None else "Luftfeuchte: N/A\n"
        
        # Schwellwert und Systemstatus
        message += f"Schwellwert: {config['warning_temp']:.1f} Grad\n"
        
        if actual_wet_temp is not None:
            frost_risk = "JA!" if actual_wet_temp <= config['warning_temp'] else "Nein"
            message += f"Frostgefahr: {frost_risk}\n"
        
        # Batterieinfos
        if readings['battery'] is not None:
            message += f"Batterie: {readings['battery']}%"
            if readings['battery_voltage'] is not None:
                message += f" ({readings['battery_voltage']:.2f}V)"
            message += "\n"
        else:
            message += "Batterie: N/A\n"
        
        # DC-DC Ausgangsspannung
        if readings['dcdc_voltage'] is not None:
            message += f"DC-DC Ausgang: {readings['dcdc_voltage']:.2f}V\n"
        else:
            message += "DC-DC Ausgang: N/A\n"
        
        # Systeminfos
        if sys_info:
            message += f"CPU: {sys_info['cpu']}%\n"
            message += f"RAM: {sys_info['memory']}%\n"
            message += f"Speicher: {sys_info['disk']}%\n"
            message += f"Laufzeit: {sys_info['uptime']}"
        
        return message
        
    except Exception as e:
        logging.error(f"Fehler beim Erstellen der Statusnachricht: {e}")
        return "Fehler beim Abrufen des Systemstatus"

def check_frost_warning():
    """Prüft auf Frostgefahr und sendet ggf. Warnungen"""
    global last_warning_time, warning_sent
    
    try:
        # Sensorwerte abrufen
        readings = last_readings
        
        # Tatsächliche Nasstemperatur festlegen (gemessen oder berechnet)
        actual_wet_temp = readings["wet_temp"]
        if actual_wet_temp is None:
            actual_wet_temp = readings["calc_wet_temp"]
        
        if actual_wet_temp is None:
            logging.warning("Keine Nasstemperatur verfügbar - keine Frostwarnung möglich")
            return False
        
        # Überprüfen auf Frostgefahr
        current_time = time.time()
        if actual_wet_temp <= config['warning_temp']:
            logging.info(f"Frost-Bedingungen erkannt: {actual_wet_temp} °C <= {config['warning_temp']} °C")
            
            # Nur warnen, wenn nicht bereits gewarnt wurde oder 3 Stunden vergangen sind
            if not warning_sent or (current_time - last_warning_time > 10800):
                dry_temp = readings['dry_temp']
                humidity = readings['humidity']
                calculated_wet_temp = readings['calc_wet_temp']
                battery = readings['battery']
                battery_voltage = readings['battery_voltage']
                dcdc_voltage = readings['dcdc_voltage']
                
                # Aktuelle Zeit für die SMS-Nachricht
                time_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

                warning_message = (
                    f"FROSTWARNUNG! ({time_str})"
                    f"\nNasstemperatur: {actual_wet_temp:.1f} Grad"
                    f"\nTrockentemp: {dry_temp:.1f} Grad" if dry_temp is not None else "\nTrockentemp: N/A"
                    f"\nLuftfeuchtigkeit: {humidity:.1f}%" if humidity is not None else "\nLuftfeuchtigkeit: N/A"
                    f"\nNasstemperatur (berechnet): {calculated_wet_temp:.1f} Grad" if calculated_wet_temp is not None else "\nNasstemperatur (berechnet): N/A"
                )
                
                # Batterieinformationen hinzufügen
                if battery is not None:
                    warning_message += f"\nBatterie: {battery}%"
                    if battery_voltage is not None:
                        warning_message += f" ({battery_voltage:.2f}V)"
                
                # DC-DC Ausgangsspannung hinzufügen
                if dcdc_voltage is not None:
                    warning_message += f"\nDC-DC Ausgang: {dcdc_voltage:.2f}V"
                
                for number in config['authorized_numbers']:
                    send_sms(number, warning_message)
                
                last_warning_time = current_time
                warning_sent = True
                logging.info("Frostwarnung gesendet")
                return True
        else:
            warning_sent = False
            
        return False
        
    except Exception as e:
        logging.error(f"Fehler bei der Frostwarnung: {e}")
        return False

def process_sms_commands():
    """Verarbeitet eingehende SMS-Befehle"""
    try:
        # SMS lesen
        sms_list = read_sms()
        
        if not sms_list:
            return
        
        for sms in sms_list:
            sender = sms['number']
            message = sms['message'].strip().upper()
            
            # Prüfen, ob der Absender autorisiert ist
            is_authorized = sender in config['authorized_numbers']
            
            # STATUS-Abfrage
            if message == config['status_code']:
                if is_authorized:
                    status_message = format_status_message()
                    send_sms(sender, status_message)
                    logging.info(f"Statusabfrage von {sender} beantwortet")
                else:
                    logging.warning(f"Nicht autorisierte Statusabfrage von {sender}")
            
            # Schwellwert ändern
            elif message.startswith(config['threshold_code']):
                if is_authorized:
                    try:
                        # Neuen Schwellwert extrahieren
                        value_str = message[len(config['threshold_code']):].strip()
                        new_threshold = float(value_str)
                        
                        # Schwellwert aktualisieren
                        old_threshold = config['warning_temp']
                        config['warning_temp'] = new_threshold
                        save_config()
                        
                        # Bestätigung senden
                        send_sms(sender, f"Schwellwert von {old_threshold}°C auf {new_threshold}°C geändert.")
                        logging.info(f"Schwellwert auf {new_threshold}°C geändert durch {sender}")
                    except Exception as e:
                        send_sms(sender, f"Fehler: Konnte Schwellwert nicht ändern. Format: {config['threshold_code']}0.5")
                        logging.error(f"Fehler bei Schwellwertänderung: {e}")
                else:
                    logging.warning(f"Nicht autorisierte Schwellwertänderung von {sender}")
            
            # System neustarten
            elif message == config['reboot_code']:
                if is_authorized:
                    time_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                    send_sms(sender, f"System wird neu gestartet... ({time_str})")
                    logging.info(f"Systemneustart durch {sender}")
                    
                    # Führe SIM800L-Reset vor dem Neustart durch
                    reset_sim800l()  # Reset des SIM800L-Moduls vor dem Neustart
                    logging.info("SIM800L Reset vor Systemneustart durchgeführt")
                    
                    # Verzögerung, damit die SMS noch gesendet werden kann
                    threading.Timer(10, lambda: os.system('sudo reboot')).start()
                else:
                    logging.warning(f"Nicht autorisierter Neustartversuch von {sender}")
            
            # SIM800L zurücksetzen
            elif message == "RESETGSM":
                if is_authorized:
                    time_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                    send_sms(sender, f"GSM-Modul wird zurückgesetzt... ({time_str})")
                    logging.info(f"GSM-Modul Reset durch {sender}")
                    
                    # Das GSM-Modul zurücksetzen
                    if reset_sim800l():
                        # Bestätigung versenden
                        send_sms(sender, "GSM-Modul wurde erfolgreich zurückgesetzt")
                    else:
                        # Fehlermeldung versenden
                        send_sms(sender, "Fehler beim Zurücksetzen des GSM-Moduls")
                else:
                    logging.warning(f"Nicht autorisierter GSM-Reset-Versuch von {sender}")
            
            # Batterie kalibrieren
            elif message == "CALIBRATE":
                if is_authorized:
                    send_sms(sender, "Spannungsmessung muss vor Ort kalibriert werden. Bitte fuehre das Skript manuell aus und verwende die Kalibrierungsoption.")
                    logging.info(f"Kalibrierungsanfrage von {sender}")
                
            # Nummer hinzufügen
            elif message.startswith(config['add_number_code']):
                if is_authorized:
                    try:
                        # Neue Nummer extrahieren
                        new_number = message[len(config['add_number_code']):].strip()
                        
                        # Nummer in internationaler Form prüfen
                        if not new_number.startswith('+'):
                            send_sms(sender, f"Fehler: Nummer muss im internationalen Format sein (+49...)")
                            continue
                        
                        # Nummer hinzufügen wenn noch nicht vorhanden
                        if new_number not in config['authorized_numbers']:
                            config['authorized_numbers'].append(new_number)
                            save_config()
                            send_sms(sender, f"Nummer {new_number} wurde hinzugefuegt.")
                            logging.info(f"Nummer {new_number} hinzugefügt durch {sender}")
                        else:
                            send_sms(sender, f"Nummer {new_number} ist bereits autorisiert.")
                    except Exception as e:
                        send_sms(sender, f"Fehler: Konnte Nummer nicht hinzufuegen.")
                        logging.error(f"Fehler beim Hinzufügen einer Nummer: {e}")
                else:
                    logging.warning(f"Nicht autorisierter Versuch, Nummer hinzuzufügen von {sender}")
            
            # Nummer entfernen
            elif message.startswith(config['remove_number_code']):
                if is_authorized:
                    try:
                        # Zu entfernende Nummer extrahieren
                        remove_number = message[len(config['remove_number_code']):].strip()
                        
                        # Prüfen, ob es die letzte Nummer ist
                        if len(config['authorized_numbers']) <= 1 and remove_number in config['authorized_numbers']:
                            send_sms(sender, "Fehler: Kann nicht die letzte autorisierte Nummer entfernen.")
                            continue
                        
                        # Nummer entfernen
                        if remove_number in config['authorized_numbers']:
                            config['authorized_numbers'].remove(remove_number)
                            save_config()
                            send_sms(sender, f"Nummer {remove_number} wurde entfernt.")
                            logging.info(f"Nummer {remove_number} entfernt durch {sender}")
                        else:
                            send_sms(sender, f"Nummer {remove_number} nicht gefunden.")
                    except Exception as e:
                        send_sms(sender, f"Fehler: Konnte Nummer nicht entfernen.")
                        logging.error(f"Fehler beim Entfernen einer Nummer: {e}")
                else:
                    logging.warning(f"Nicht autorisierter Versuch, Nummer zu entfernen von {sender}")
            
            # Hilfe anzeigen
            elif message == config['help_code']:
                if is_authorized:
                    help_message = f"""Befehle:
{config['status_code']} - Systemstatus abfragen
{config['threshold_code']}X - Schwellwert auf X Grad setzen
{config['add_number_code']}+49... - Nummer hinzufuegen
{config['remove_number_code']}+49... - Nummer entfernen
{config['reboot_code']} - System neustarten
RESETGSM - GSM-Modul zurücksetzen
CALIBRATE - Infos zur Spannungskalibrierung
{config['help_code']} - Diese Hilfe anzeigen"""
                    send_sms(sender, help_message)
                    logging.info(f"Hilfe an {sender} gesendet")
                else:
                    logging.warning(f"Nicht autorisierte Hilfeanfrage von {sender}")
            
            # Unbekannter Befehl
            elif is_authorized:
                send_sms(sender, f"Unbekannter Befehl. Sende '{config['help_code']}' fuer eine Liste der Befehle.")
                logging.info(f"Unbekannter Befehl von {sender}: {message}")
    
    except Exception as e:
        logging.error(f"Fehler bei der SMS-Verarbeitung: {e}")

def sms_service_loop():
    """Schleife für den SMS-Service"""
    logging.info("SMS-Service-Schleife gestartet")
    
    last_error_count = 0
    while True:
        try:
            # SMS-Befehle verarbeiten
            process_sms_commands()
            
            # Kurze Pause
            time.sleep(config['sms_check_interval'])
            
            # Fehler zurücksetzen, wenn erfolgreich
            if last_error_count > 0:
                logging.info("SMS-Service wiederhergestellt nach Fehlern")
                last_error_count = 0
                
        except Exception as e:
            last_error_count += 1
            logging.error(f"Fehler in der SMS-Service-Schleife: {e} (Fehler #{last_error_count})")
            
            # Nach zu vielen Fehlern versuchen, das GSM-Modul neu zu initialisieren
            if last_error_count >= 5:
                logging.warning("Zu viele Fehler im SMS-Service - Versuche GSM-Modul neu zu initialisieren")
                try:
                    global gsm
                    
                    # Führe einen Hardware-Reset durch, bevor wir die serielle Verbindung neu initialisieren
                    reset_sim800l()
                    logging.info("SIM800L Hardware-Reset durchgeführt")
                    
                    # Serielle Verbindung schließen und neu öffnen
                    if gsm:
                        gsm.close()
                    time.sleep(2)
                    gsm = serial.Serial('/dev/ttyS0', 9600, timeout=1)
                    send_command('AT')  # Test-Befehl
                    logging.info("GSM-Modul erfolgreich neu initialisiert")
                    last_error_count = 0
                except Exception as reinit_error:
                    logging.error(f"Konnte GSM-Modul nicht neu initialisieren: {reinit_error}")
            
            time.sleep(60)  # Bei Fehler länger warten

def sensor_monitoring_loop():
    """Schleife für die Temperaturüberwachung"""
    global warning_sent
    
    logging.info("Temperaturüberwachung gestartet")
    
    consecutive_errors = 0
    last_battery_warning = 0
    
    while True:
        try:
            # Sensordaten aktualisieren
            readings = update_sensor_data()
            
            if readings is None:
                # Fehler beim Abrufen der Sensordaten
                consecutive_errors += 1
                logging.warning(f"Fehler beim Abrufen der Sensordaten (#{consecutive_errors})")
                
                if consecutive_errors >= 5:
                    error_message = "Systemwarnung: Mehrere Fehler beim Lesen der Sensordaten!"
                    for number in config['authorized_numbers']:
                        send_sms(number, error_message)
                    logging.error("SMS-Warnung über Sensorfehler gesendet")
                    consecutive_errors = 0  # Zurücksetzen, um nicht ständig zu warnen
                
                time.sleep(60)  # Kurz warten und neu versuchen
                continue
            
            # Erfolgreich Daten gelesen, Fehlerzähler zurücksetzen
            consecutive_errors = 0
            
            # Frostwarnung prüfen
            frost_warning = check_frost_warning()
            
            # Messintervall festlegen
            wet_temp = readings["wet_temp"]
            calc_wet_temp = readings["calc_wet_temp"]
            
            if frost_warning or (wet_temp is not None and wet_temp <= config['warning_temp']) or \
               (calc_wet_temp is not None and calc_wet_temp <= config['warning_temp']):
                # Bei kritischen Temperaturen häufiger messen
                sleep_time = config['check_interval_critical']
                logging.info(f"Kritische Temperatur - nächste Messung in {sleep_time/60} Minuten")
            else:
                sleep_time = config['check_interval']
                logging.info(f"Normale Temperatur - nächste Messung in {sleep_time/60} Minuten")
            
            # Batterie-Warnung bei niedrigem Stand (höchstens einmal alle 12 Stunden)
            battery_level = readings["battery"]
            current_time = time.time()
            
            if battery_level is not None and battery_level < config.get('battery_warning_level', 20) and (current_time - last_battery_warning > 43200):
                battery_voltage = readings["battery_voltage"]
                dcdc_voltage = readings["dcdc_voltage"]
                
                battery_message = f"Warnung: Niedriger Batteriestand ({battery_level}%)!"
                
                # Spannungsinformationen hinzufügen
                if battery_voltage is not None:
                    battery_message += f" Batterie: {battery_voltage:.2f}V"
                if dcdc_voltage is not None:
                    battery_message += f", DC-DC: {dcdc_voltage:.2f}V"
                
                for number in config['authorized_numbers']:
                    send_sms(number, battery_message)
                logging.warning(f"Niedriger Batteriestand: {battery_level}%")
                last_battery_warning = current_time
            
            # Bei kritisch niedrigem Batteriestand (< 10%) häufiger messen
            if battery_level is not None and battery_level < config.get('battery_critical_level', 10):
                logging.warning("Kritisch niedriger Batteriestand - Energiesparmodus aktiviert")
                sleep_time = max(sleep_time, 1800)  # Mindestens 30 Minuten Intervall zum Energiesparen
            
            time.sleep(sleep_time)
            
        except Exception as e:
            consecutive_errors += 1
            logging.error(f"Fehler in der Temperaturüberwachung: {e} (Fehler #{consecutive_errors})")
            
            # Bei zu vielen aufeinanderfolgenden Fehlern eine Warnung senden
            if consecutive_errors >= 5:
                error_message = f"Systemwarnung: Mehrere Fehler in der Temperaturueberwachung: {str(e)}"
                for number in config['authorized_numbers']:
                    send_sms(number, error_message)
                logging.error("SMS-Warnung über Sensorfehler gesendet")
                consecutive_errors = 0  # Zurücksetzen, um nicht ständig zu warnen
            
            time.sleep(60)  # Bei Fehler kurz warten und neu versuchen

def main():
    """Hauptfunktion"""
    try:
        # Konfiguration laden
        load_config()
        
        # Banner und Initialisierungsmeldung
        print("=" * 50)
        print("Integriertes Frostwarnsystem mit SMS-Service")
        print("=" * 50)
        
        # Hardware-Status prüfen
        logging.info("Frostwarnsystem wird gestartet...")
        
        if DRY_SENSOR:
            logging.info(f"Trockentemperatursensor gefunden: {DRY_SENSOR}")
        else:
            logging.warning("Kein Trockentemperatursensor gefunden!")
        
        if WET_SENSOR:
            logging.info(f"Nasstemperatursensor gefunden: {WET_SENSOR}")
        else:
            logging.warning("Kein Nasstemperatursensor gefunden!")
        
        init_dht_sensor()
        if DHT_SENSOR:
            logging.info("DHT22 Sensor initialisiert")
        else:
            logging.warning("DHT22 Sensor nicht verfügbar")
        
        if gsm:
            logging.info("GSM-Modul initialisiert")
        else:
            logging.error("GSM-Modul nicht verfügbar - System kann keine SMS senden oder empfangen")
            print("FEHLER: GSM-Modul nicht verfügbar")
        
        # Batteriemessungs-Hardware initialisieren
        battery_monitor_available = init_battery_monitor()
        if battery_monitor_available:
            logging.info("Batterie-Monitoring aktiv")
            
            # Prüfen, ob Kalibrierung gewünscht ist
            if 'battery_calibration_factor' not in config:
                print("\nERSTE KALIBRIERUNG DER BATTERIEMESSUNG ERFORDERLICH")
                print("Ohne Kalibrierung können die Batteriewerte ungenau sein.")
                calibrate = input("Möchtest du die Batteriemessung jetzt kalibrieren? (j/n): ").lower()
                if calibrate in ('j', 'ja'):
                    if calibrate_battery_sensor():
                        print("Kalibrierung erfolgreich.")
                    else:
                        print("Kalibrierung fehlgeschlagen, verwende Standardwerte.")
                        config['battery_calibration_factor'] = 1.0
                        save_config()
                else:
                    print("Kalibrierung übersprungen, verwende Standardwerte.")
                    config['battery_calibration_factor'] = 1.0
                    save_config()
            
            # Prüfen, ob DC-DC-Kalibrierung gewünscht ist
            if 'dcdc_calibration_factor' not in config:
                print("\nERSTE KALIBRIERUNG DER DC-DC-AUSGANGSSPANNUNG ERFORDERLICH")
                print("Ohne Kalibrierung können die DC-DC-Werte ungenau sein.")
                calibrate = input("Möchtest du die DC-DC-Spannungsmessung jetzt kalibrieren? (j/n): ").lower()
                if calibrate in ('j', 'ja'):
                    if calibrate_dcdc_sensor():
                        print("Kalibrierung erfolgreich.")
                    else:
                        print("Kalibrierung fehlgeschlagen, verwende Standardwerte.")
                        config['dcdc_calibration_factor'] = 1.0
                        save_config()
                else:
                    print("Kalibrierung übersprungen, verwende Standardwerte.")
                    config['dcdc_calibration_factor'] = 1.0
                    save_config()
        else:
            logging.warning("Batterie-Monitoring nicht verfügbar")
        
        # Initialer Systemstatus abrufen
        update_sensor_data()
        
        # Startmeldung per SMS
        if gsm:
            time_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
            for number in config['authorized_numbers']:
                send_sms(number, f"Frostwarnsystem wurde neu gestartet ({time_str}). Sende STATUS für aktuelle Werte.")
                
        # Threads starten
        sms_thread = threading.Thread(target=sms_service_loop, daemon=True)
        sensor_thread = threading.Thread(target=sensor_monitoring_loop, daemon=True)
        
        sms_thread.start()
        sensor_thread.start()
        
        logging.info("Alle Threads gestartet, System läuft")
        print("System läuft...")
        
        # Threads am Leben halten
        while True:
            # Prüfen, ob beide Threads noch laufen
            if not sms_thread.is_alive():
                logging.error("SMS-Thread ist gestorben - Neustart")
                sms_thread = threading.Thread(target=sms_service_loop, daemon=True)
                sms_thread.start()
            
            if not sensor_thread.is_alive():
                logging.error("Sensor-Thread ist gestorben - Neustart")
                sensor_thread = threading.Thread(target=sensor_monitoring_loop, daemon=True)
                sensor_thread.start()
            
            time.sleep(60)  # Alle 60 Sekunden prüfen
            
    except Exception as e:
        logging.critical(f"Kritischer Fehler im Hauptprogramm: {e}")
        # Versuche, einen Neustart auszulösen
        try:
            for number in config['authorized_numbers']:
                send_sms(number, f"Kritischer Fehler im Frostwarnsystem: {e} - Versuche Neustart...")
            
            time.sleep(10)
            
            # SIM800L zurücksetzen, bevor wir den Raspberry Pi neustarten
            reset_sim800l()
            logging.info("SIM800L zurückgesetzt vor Systemneustart nach kritischem Fehler")
            
            os.system('sudo reboot')
        except:
            pass

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Programm beendet.")
    finally:
        GPIO.cleanup()