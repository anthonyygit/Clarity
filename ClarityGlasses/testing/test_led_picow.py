from machine import Pin
import time

led = Pin("LED", Pin.OUT)

print("Testing Pico W LED...")

while True:
    led.on()
    print("LED ON")
    time.sleep(1)

    led.off()
    print("LED OFF")
    time.sleep(1)
