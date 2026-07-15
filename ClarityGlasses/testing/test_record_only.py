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

print("Press button to record...")

while True:
    if not button.value():
        led.on()
        print("Recording 3 seconds...")

        start = time.time()
        chunks = 0
        total_bytes = 0
        chunk_size = 4096

        with open("audio.raw", "wb") as f:
            while time.time() - start < 3:
                chunk = bytearray(chunk_size)
                try:
                    bytes_read = i2s.readinto(chunk)
                    if bytes_read and bytes_read > 0:
                        f.write(chunk[:bytes_read])
                        chunks += 1
                        total_bytes += bytes_read
                        print(".", end="")
                    else:
                        print("?", end="")
                except Exception as e:
                    print(f"Error: {e}")
                    break

        elapsed = time.time() - start
        print(f"\nSaved {chunks} chunks, {total_bytes} bytes to audio.raw in {elapsed:.1f}s")

        led.off()
        time.sleep(1)
