from machine import Pin
import time

led = Pin(25, Pin.OUT)
button = Pin(11, Pin.IN, Pin.PULL_DOWN)

print("Testing button on GP10...")

while True:
    val = button.value()
    print(f"Button: {val}")

    if val:
        led.on()
        print("LED ON")
    else:
        led.off()
        print("LED OFF")

    time.sleep(0.5)
