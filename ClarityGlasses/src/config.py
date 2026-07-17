# Shared config for glasses code + commands

# Flip this between "hotspot" and "wifi" depending on where you're testing —
# each mode remembers its own network + backend IP, so switching is a
# one-line change instead of retyping everything. If a backend connection
# ever fails, check the Mac's current IP on that network (ipconfig getifaddr
# en0, or whatever interface is active) and update the matching *_BACKEND_URL
# below — these drift whenever the network reassigns an address.
NETWORK_MODE = "wifi"  # "hotspot" or "wifi"

HOTSPOT_SSID = "Anthony"
HOTSPOT_PASSWORD = "anthonyy"
HOTSPOT_BACKEND_URL = "http://172.20.10.4:8000"

WIFI_SSID = "Stan"
WIFI_PASSWORD = "parisparis123"
WIFI_BACKEND_URL = "http://192.168.68.51:8000"

if NETWORK_MODE == "hotspot":
    SSID = HOTSPOT_SSID
    PASSWORD = HOTSPOT_PASSWORD
    BACKEND_URL = HOTSPOT_BACKEND_URL
else:
    SSID = WIFI_SSID
    PASSWORD = WIFI_PASSWORD
    BACKEND_URL = WIFI_BACKEND_URL

# Speaker volume: 1.0 = as-is, 0.5 = half, up to 4.0 (boosted, may clip)
VOLUME = .5

# Voice detection
VAD_FLOOR = 35   # minimum speech threshold (lower = hears softer speech)
VAD_MULT = 2.5   # threshold = ambient_noise * VAD_MULT (raise if it false-triggers)

# Sound effects — WAV files on the Pico's own filesystem (played instantly,
# no network needed). Set to None to disable either one. Paths are relative
# to /src unless you give an absolute path.
SFX_BOOT = "/src/sfx/boot.wav"
SFX_BUTTON = "/src/sfx/button.wav"
SFX_PROCESSING = "/src/sfx/processing.wav"  # one-shot, plays once you stop talking
SFX_THINKING = "/src/sfx/thinking.wav"      # looped while waiting on the backend
