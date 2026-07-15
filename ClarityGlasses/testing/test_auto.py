from machine import Pin, I2S
import network
import urequests
import time
import gc

SSID = "Stan"
PASSWORD = "parisparis123"
BACKEND_URL = "http://192.168.68.84:8000"
SAMPLE_RATE = 16000
BYTES_PER_SEC = SAMPLE_RATE * 2
BATCH_SIZE = BYTES_PER_SEC
IBUF_SIZE = 64000
SECONDS = 3

batch = bytearray(BATCH_SIZE)
batch_mv = memoryview(batch)

print("AUTO-TEST: connecting wifi...")
wlan = network.WLAN(network.STA_IF)
wlan.active(True)
if not wlan.isconnected():
    wlan.connect(SSID, PASSWORD)
    for _ in range(30):
        if wlan.isconnected():
            break
        time.sleep(0.5)
print("wifi:", wlan.isconnected(), wlan.ifconfig()[0] if wlan.isconnected() else "")

i2s = I2S(0, sck=Pin(16), ws=Pin(17), sd=Pin(18),
          mode=I2S.RX, bits=16, format=I2S.MONO,
          rate=SAMPLE_RATE, ibuf=IBUF_SIZE)

junk = bytearray(4096)
i2s.readinto(junk)

try:
    urequests.post(BACKEND_URL + "/transcribe/reset").close()
except Exception as e:
    print("reset err:", e)

total = 0
t0 = time.ticks_ms()
print("recording %ds..." % SECONDS)
for _ in range(SECONDS):
    n = i2s.readinto(batch_mv)
    if n:
        total += n
        try:
            r = urequests.post(BACKEND_URL + "/transcribe", data=batch_mv[:n])
            r.close()
        except Exception as e:
            print("http:", e)
        gc.collect()

wall = time.ticks_diff(time.ticks_ms(), t0) / 1000
print("RESULT: captured %dB = %.2fs audio in %.2fs wall (%.0f%%)"
      % (total, total / BYTES_PER_SEC, wall, 100 * total / (SECONDS * BYTES_PER_SEC)))

try:
    r = urequests.post(BACKEND_URL + "/transcribe/done", timeout=30)
    print("transcript:", r.json().get("transcript"))
    r.close()
except Exception as e:
    print("done err:", e)

i2s.deinit()
print("AUTO-TEST COMPLETE")
