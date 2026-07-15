from machine import Pin, I2S
import time
import ubinascii

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
        print("=== START AUDIO HEX ===")

        start = time.time()
        chunk_num = 0

        while time.time() - start < 3:
            chunk = bytearray(4096)
            try:
                bytes_read = i2s.readinto(chunk)
                if bytes_read and bytes_read > 0:
                    hex_data = ubinascii.hexlify(chunk[:bytes_read]).decode()
                    print(hex_data)
                    chunk_num += 1
            except Exception as e:
                print(f"Error: {e}")
                break

        print("=== END AUDIO HEX ===\n")
        led.off()
        time.sleep(1)
