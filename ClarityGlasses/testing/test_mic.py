from machine import Pin, I2S
import time

led = Pin("LED", Pin.OUT)

print("Testing I2S microphone...")

i2s = I2S(
    0,
    sck=Pin(16),
    ws=Pin(17),
    sd=Pin(18),
    mode=0,
    bits=16,
    format=0,
    rate=16000,
    ibuf=10000,
)

print("Recording 3 seconds...")
start = time.time()
chunk_size = 4096
data_size = 0

while time.time() - start < 3:
    chunk = bytearray(chunk_size)
    bytes_read = i2s.readinto(chunk)
    if bytes_read:
        data_size += bytes_read
        print(f"Read {bytes_read} bytes")
    led.on()
    time.sleep(0.1)
    led.off()

print(f"Done! Recorded {data_size} bytes")
