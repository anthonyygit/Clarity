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

        start = time.time()
        audio_data = bytearray()
        chunk_size = 4096

        while time.time() - start < 3:
            chunk = bytearray(chunk_size)
            try:
                bytes_read = i2s.readinto(chunk)
                if bytes_read and bytes_read > 0:
                    audio_data.extend(chunk[:bytes_read])
                    print(".", end="")
            except Exception as e:
                print(f"Error: {e}")
                break

        print("\nEncoding to base64...")
        b64_data = ubinascii.b2a_base64(audio_data).decode()

        print("\n=== AUDIO DATA (BASE64) ===")
        print(b64_data)
        print("=== END AUDIO DATA ===\n")

        led.off()
        time.sleep(1)
