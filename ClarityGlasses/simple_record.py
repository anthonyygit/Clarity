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

        filename = "audio.raw"
        with open(filename, "wb") as f:
            for i in range(6):
                chunk = bytearray(8192)
                i2s.readinto(chunk)
                f.write(chunk)
                print(i, end=" ")

        print("\nDone! Saved to audio.raw")
        led.off()
        time.sleep(1)
