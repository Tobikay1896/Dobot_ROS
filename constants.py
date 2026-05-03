# omni/mazerunner/constants.py
from pxr import Gf

# -------------------------------------------------------------------------
# Farben (ARGB – 0xFFRRGGBB)
# -------------------------------------------------------------------------
CLR_BG_DARK    = 0xFF0A0F1A
CLR_BG_MID     = 0xFF101828
CLR_BG_ROW_A   = 0xFF121E30
CLR_BG_ROW_B   = 0xFF162438
CLR_BG_HEADER  = 0xFF0D1420
CLR_ACCENT     = 0xFF4A9EFF
CLR_GREEN      = 0xFF4ADE80
CLR_RED        = 0xFFEF6B6B
CLR_YELLOW     = 0xFFE0B040
CLR_ORANGE     = 0xFFE08040
CLR_TEXT       = 0xFFD0D8E8
CLR_TEXT_DIM   = 0xFF607090
CLR_TEXT_FAINT = 0xFF405070
CLR_BORDER     = 0xFF1A2840

MAX_LOG_LINES = 80

# -------------------------------------------------------------------------
# Pfade & Startposition des Deckels
# -------------------------------------------------------------------------
DECKEL_PRIM_PATH = "/World/Production_Line/Deckelmagazin/Deckel"
DECKEL_START_POS = Gf.Vec3f(-67.35224, -0.0526, 6.33423)

# -------------------------------------------------------------------------
# MQTT‑Konfiguration
# -------------------------------------------------------------------------
MQTT_BROKER   = "digitaltwinservice.de"
MQTT_PORT     = 1883
MQTT_KEEPALIVE = 60