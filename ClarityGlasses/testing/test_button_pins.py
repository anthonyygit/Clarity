from machine import Pin
import time

led = Pin("LED", Pin.OUT)

pins_to_test = [10, 11, 12, 13, 14]

for pin_num in pins_to_test:
    print(f"\nTesting pin {pin_num}...")
    button = Pin(pin_num, Pin.IN, Pin.PULL_DOWN)

    for _ in range(5):
        val = button.value()
        if not val:
            print(f"  Pin {pin_num}: LOW (button pressed!)")
            led.on()
            time.sleep(0.2)
            led.off()
        else:
            print(f"  Pin {pin_num}: high")
        time.sleep(0.5)
