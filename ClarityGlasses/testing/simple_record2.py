from machine import Pin, I2S
import time

led = Pin("LED", Pin.OUT)
button = Pin(10, Pin.IN, Pin.PULL_UP)

i2s = I2S(
    0,
    sck=Pin(16),
    ws=Pin(17),
    sd=Pin(18),
    mode=0,
    bits=16,
    format=0,
    rate=16000,
    ibuf=40000,
)

print("Waiting for button press...")

while True:
    if not button.value():
        print("Recording 3 seconds...")
        led.on()

        start = time.time()
        chunk_size = 8192
        total = 0

        while time.time() - start < 3:
            chunk = bytearray(chunk_size)
            bytes_read = i2s.readinto(chunk)
            if bytes_read:
                total += bytes_read
                print(".", end="")
            time.sleep(0.01)

        print(f"\nDone! Recorded {total} bytes")
        led.off()
        time.sleep(1)
