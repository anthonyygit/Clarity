from machine import Pin, I2S
import network
import time
import gc

SSID = "Stan"
PASSWORD = "parisparis123"

led = Pin("LED", Pin.OUT)
button = Pin(10, Pin.IN, Pin.PULL_UP)

def init_wifi():
    print("Connecting to WiFi...")
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(SSID, PASSWORD)
    for i in range(20):
        if wlan.isconnected():
            print(f"WiFi: {wlan.ifconfig()[0]}")
            return wlan
        time.sleep(0.5)
    return None

def test_rate(rate, duration=5):
    print(f"\n=== Testing {rate}Hz for {duration}s ===")
    try:
        i2s = I2S(
            0,
            sck=Pin(16),
            ws=Pin(17),
            sd=Pin(18),
            mode=I2S.RX,
            bits=16,
            format=I2S.MONO,
            rate=rate,
            ibuf=40000,
        )

        led.on()
        start = time.time()
        total = 0
        chunks = 0

        while time.time() - start < duration:
            chunk = bytearray(4096)
            n = i2s.readinto(chunk)
            if n and n > 0:
                total += n
                chunks += 1
            time.sleep(0.01)

        led.off()
        elapsed = time.time() - start

        expected = rate * duration * 2

        print(f"Got: {total}B in {elapsed:.1f}s ({chunks} chunks)")
        print(f"Expected @ {rate}Hz: {expected}B")
        print(f"Ratio: {total/expected:.2%}")

        i2s.deinit()
        gc.collect()

    except Exception as e:
        print(f"Error: {e}")
        led.off()

print("=== Pico I2S Rate Test ===")
wlan = init_wifi()

if not wlan:
    print("WiFi failed, but continuing with I2S tests...")

print("Press button to start tests")
while button.value():
    time.sleep(0.1)

time.sleep(1)

rates = [4000, 6000, 8000, 10000, 12000, 16000, 32000]
for r in rates:
    test_rate(r, duration=3)
    time.sleep(1)

print("\n=== Tests Complete ===")
print("Check which rate gave closest to 100% expected data")
