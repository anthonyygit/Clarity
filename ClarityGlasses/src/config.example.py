
NETWORK_MODE = "hotspot"

HOTSPOT_SSID = "your_hotspot_name"
HOTSPOT_PASSWORD = "your_hotspot_password"
HOTSPOT_BACKEND_URL = "http://<mac_ip_on_hotspot>:8000"

WIFI_SSID = "your_wifi_name"
WIFI_PASSWORD = "your_wifi_password"
WIFI_BACKEND_URL = "http://<mac_ip_on_wifi>:8000"

if NETWORK_MODE == "hotspot":
    SSID = HOTSPOT_SSID
    PASSWORD = HOTSPOT_PASSWORD
    BACKEND_URL = HOTSPOT_BACKEND_URL
else:
    SSID = WIFI_SSID
    PASSWORD = WIFI_PASSWORD
    BACKEND_URL = WIFI_BACKEND_URL

VOLUME = 1.0

VAD_FLOOR = 35
VAD_MULT = 2.5

SFX_BOOT = "/src/sfx/boot.wav"
SFX_BUTTON = "/src/sfx/button.wav"
SFX_PROCESSING = "/src/sfx/processing.wav"
SFX_THINKING = "/src/sfx/thinking.wav"
