# Shared config for glasses code + commands
SSID = "Anthony"
PASSWORD = "anthonyy"
BACKEND_URL = "http://172.20.10.4:8000"

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
