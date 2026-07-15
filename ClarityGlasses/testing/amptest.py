from machine import Pin, I2S
import network
import urequests
import time

SSID = "Stan"
PASSWORD = "parisparis123"
BACKEND_URL = "http://192.168.68.84:8000"

led = Pin("LED", Pin.OUT)
button = Pin(10, Pin.IN, Pin.PULL_UP)

i2s = I2S(
    0,
    sck=Pin(16),
    ws=Pin(17),
    sd=Pin(18),
    mode=0,
    bits=16,
    format=1,
    rate=16000,
    ibuf=40000,
)

def connect_wifi():
    print("Connecting to WiFi...")
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(SSID, PASSWORD)
    for i in range(10):
        if wlan.isconnected():
            print("WiFi connected")
            led.on()
            time.sleep(0.5)
            led.off()
            return wlan
        print(f"Waiting... {i+1}/10")
        time.sleep(1)
    print("WiFi timeout")
    return wlan

def amplify_audio(data, gain=2):
    import struct
    amplified = bytearray(len(data))
    for i in range(0, len(data), 2):
        sample = struct.unpack('<h', data[i:i+2])[0]
        sample = int(sample * gain)
        sample = max(-32768, min(32767, sample))
        struct.pack_into('<h', amplified, i, sample)
    return bytes(amplified)

def record_and_send(duration=10):
    chunk_size = 4096
    start_time = time.time()
    chunks_sent = 0
    total_bytes = 0
    batch_buffer = bytearray()
    bytes_per_second = 16000 * 2

    print(f"Recording for {duration} seconds...")
    while time.time() - start_time < duration:
        chunk = bytearray(chunk_size)
        try:
            bytes_read = i2s.readinto(chunk)
            elapsed = time.time() - start_time
            
            if bytes_read and bytes_read > 0:
                batch_buffer.extend(chunk[:bytes_read])
                total_bytes += bytes_read
                expected_bytes = int(elapsed * bytes_per_second)
                print(f"[{elapsed:.1f}s] Read {bytes_read}B (Total: {total_bytes}B, Expected: {expected_bytes}B)")

                if len(batch_buffer) >= chunk_size:
                    try:
                        amplified_data = amplify_audio(bytes(batch_buffer), gain=1)
                        response = urequests.post(f"{BACKEND_URL}/transcribe", data=amplified_data)
                        response.close()
                        chunks_sent += 1
                        print(f"  → SENT chunk {chunks_sent}")
                        batch_buffer = bytearray()
                    except Exception as e:
                        print(f"[HTTP Error: {e}]")
            time.sleep(0.01)
        except Exception as e:
            print(f"[I2S Error: {e}]")

    if len(batch_buffer) > 0:
        try:
            amplified_data = amplify_audio(bytes(batch_buffer), gain=1)
            response = urequests.post(f"{BACKEND_URL}/transcribe", data=amplified_data)
            response.close()
            chunks_sent += 1
        except Exception as e:
            print(f"[Final Error: {e}]")

    print(f"✓ Done! Sent {chunks_sent} chunks, {total_bytes} total bytes in {time.time()-start_time:.1f}s")
    
    
def get_transcript():
    try:
        print("Getting transcript...")
        response = urequests.post(f"{BACKEND_URL}/transcribe/done", timeout=30)
        if response.status_code == 200:
            data = response.json()
            text = data.get("transcript", "")
            response.close()
            return text
        response.close()
    except Exception as e:
        print(f"Error: {e}")
    return None


print("Starting...")
connect_wifi()

button_was_pressed = False

while True:
    if not button.value() and not button_was_pressed:
        button_was_pressed = True
        led.on()

        record_and_send(duration=10)

        result = get_transcript()
        if result:
            print(f"Transcript: {result}")

        led.off()
        time.sleep(0.5)

    if button.value():
        button_was_pressed = False

    time.sleep(0.05)
