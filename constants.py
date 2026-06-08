"""
constants.py
============
Zentrale Konstanten für die gesamte Extension.
Hier liegen alle Farbwerte, USD-Pfade, MQTT-Settings und magische Zahlen,
damit sie an EINER Stelle gepflegt werden können.
"""

# -------------------------------------------------------------------------
# Farben (ARGB-Hex) für die UI
# -------------------------------------------------------------------------
CLR_BG_DARK     = 0xFF0A0F1A   # Sehr dunkler Hintergrund (Header)
CLR_BG_MID      = 0xFF101828   # Mittlerer Hintergrund (Toolbar, Liste)
CLR_BG_ROW_A    = 0xFF121E30   # Zeilenfarbe gerade
CLR_BG_ROW_B    = 0xFF162438   # Zeilenfarbe ungerade
CLR_BG_HEADER   = 0xFF0D1420   # Spaltenkopf
CLR_ACCENT      = 0xFF4A9EFF   # Akzentfarbe (Info)
CLR_GREEN       = 0xFF4ADE80   # OK / TRUE
CLR_RED         = 0xFFEF6B6B   # Fehler / FALSE
CLR_YELLOW      = 0xFFE0B040   # Warnung / pending
CLR_ORANGE      = 0xFFE08040   # Velocity-Impuls aktiv
CLR_TEXT        = 0xFFD0D8E8   # Primärtext
CLR_TEXT_DIM    = 0xFF607090   # Sekundärtext
CLR_TEXT_FAINT  = 0xFF405070   # Hint / sehr leise
CLR_BORDER      = 0xFF1A2840   # Trennlinien

# -------------------------------------------------------------------------
# Log-Konfiguration
# -------------------------------------------------------------------------
MAX_LOG_LINES = 80   # Maximale Anzahl Log-Zeilen im UI

# -------------------------------------------------------------------------
# MQTT-Konfiguration (entspricht Unity-Setup)
# -------------------------------------------------------------------------
MQTT_BROKER     = "digitaltwinservice.de"
MQTT_PORT       = 1883
MQTT_KEEPALIVE  = 60

# -------------------------------------------------------------------------
# REST-API-Endpunkte
# -------------------------------------------------------------------------
API_URL_GET = "https://digitaltwinservice.de/api/Database/GetValue"
API_URL_SET = "https://digitaltwinservice.de/api/Database/SetValue"
API_KEY     = "2b56f658-b11f-4067-9537-631bf27a30f0"

# -------------------------------------------------------------------------
# USD-Pfade für den Sauggreifer und das Pick-/Place-Setup
# -------------------------------------------------------------------------
SUCTION_TARGET_PATH       = "/World/Production_Line/Deckelmagazin/Deckel"
SUCTION_GRIPPER_MESH_PATH = (
    "/World/Production_Line/Schwenkarm_Deckel/"
    "Schwenkarm_Deckel_move_translatory/"
    "Schwenkarm_Deckel_move_rotatory/"
    "tn__Saubnapf1_zH/tn__Volumenkrper2_gm2/Mesh"
)
SUCTION_JOINT_PATH        = "/World/Production_Line/SaugnapfDeckelJoint"
PLACE_TARGET_PATH         = "/World/Production_Line/Mazemagazin/Maze"

# -------------------------------------------------------------------------
# Geometrische Offsets für das Aufsetzen des Deckels
# -------------------------------------------------------------------------
PLACE_OFFSET_X      = 0.0
PLACE_OFFSET_Y      = 0.0
PLACE_OFFSET_Z      = 0.0
DECKEL_HALF_HEIGHT  = 0.0025   # 5 mm / 2 → halbe Deckelhöhe
SAFETY_OFFSET       = 0.0005   # 0.5 mm Sicherheitsabstand

# -------------------------------------------------------------------------
# Deckel-Startposition (lokal relativ zu /World/Production_Line/Deckelmagazin)
# Aus dem Isaac-Sim-Property-Panel abgelesen. Wird beim Reset als Ziel gesetzt.
# -------------------------------------------------------------------------
DECKEL_START_LOCAL_POS = (-72.15972, -0.93587, 7.15066)
DECKEL_START_LOCAL_ROT = (0.0, 0.0, 0.0)   # Euler XYZ
