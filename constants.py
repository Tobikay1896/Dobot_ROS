"""
constants.py
============
Zentrale Konstanten für die Extension.
"""

# -------------------------------------------------------------------------
# Farben (ARGB-Hex) für die UI
# -------------------------------------------------------------------------
CLR_BG_DARK  = 0xFF0A0F1A   # Sehr dunkler Hintergrund
CLR_BG_MID   = 0xFF101828   # Aktives Panel / Toolbar
CLR_ACCENT   = 0xFF4A9EFF   # Akzentfarbe (Info)
CLR_GREEN    = 0xFF4ADE80   # OK / Verbunden
CLR_RED      = 0xFFEF6B6B   # Fehler / Getrennt
CLR_YELLOW   = 0xFFE0B040   # Warnung / Pending
CLR_TEXT     = 0xFFD0D8E8   # Primärtext
CLR_TEXT_DIM = 0xFF607090   # Sekundärtext
CLR_BORDER   = 0xFF1A2840   # Trennlinien

# -------------------------------------------------------------------------
# USD-Pfad des Dobot-Roboters in der Szene
# -------------------------------------------------------------------------
ROBOT_USD_PATH = "/World/magician/base_link"

# -------------------------------------------------------------------------
# Nullposition / Home-Position (kartesisch, in mm / Grad)
# -------------------------------------------------------------------------
HOME_X =  -3.7
HOME_Y = 227.8
HOME_Z =  66.1
HOME_R =  88.4

# -------------------------------------------------------------------------
# Servo an GP3 (RC-Servo, PWM 50 Hz)
# GP3-PWM1 = IO-Adresse 15 laut Pinout-Tabelle.
# -------------------------------------------------------------------------
GP3_PWM_ADDRESS = 15     # IO-Adresse des PWM1-Pins auf GP3
SERVO_FREQ_HZ   = 50.0   # Trägerfrequenz Standard-RC-Servo
SERVO_MIN_DUTY  = 0.05   # 1 ms Pulsbreite  →   0°
SERVO_MAX_DUTY  = 0.10   # 2 ms Pulsbreite  → 180°
