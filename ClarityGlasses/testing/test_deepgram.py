import network
import urequests
import time

SSID = "Stan"
PASSWORD = "parisparis123"
DEEPGRAM_API_KEY = "425bf2c5326210cb87b5c0e0b20a594eedb2b41f"

print("Connecting WiFi...")
wlan = network.WLAN(network.STA_IF)
wlan.active(True)
wlan.connect(SSID, PASSWORD)

for i in range(10):
    if wlan.isconnected():
        print("Connected!")
        break
    time.sleep(1)

print("Creating dummy audio...")
dummy_audio = b'\x00' * 32000

print("Sending to Deepgram...")
url = "https://api.deepgram.com/v1/listen?model=nova-2&encoding=linear16&sample_rate=16000"
headers = {
    "Authorization": f"Token {DEEPGRAM_API_KEY}",
    "Content-Type": "audio/raw"
}

try:
    response = urequests.post(url, data=dummy_audio, headers=headers, timeout=30)
    print(f"Status: {response.status_code}")
    print(f"Response: {response.text[:200]}")
    response.close()
except Exception as e:
    print(f"Error: {e}")
