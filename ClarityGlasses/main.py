from machine import Pin, I2S
import network
import urequests
import time
import gc
import sys

try:
    import _thread
except ImportError:
    _thread = None


for _p in ("/src", "/src/lib"):
    if _p not in sys.path:
        sys.path.append(_p)

import config
from config import SSID, PASSWORD, BACKEND_URL

VAD_FLOOR = getattr(config, "VAD_FLOOR", 70)
VAD_MULT = getattr(config, "VAD_MULT", 4)

SAMPLE_RATE = 16000
BYTES_PER_SEC = SAMPLE_RATE * 2         
CHUNK_SIZE = 8000
BATCH_SIZE = BYTES_PER_SEC
IBUF_SIZE = 64000

SPEECH_THRESHOLD = 150
ambient_level = 30
SILENCE_STOP_MS = 2000
MIN_SPEECH_MS = 600
NO_SPEECH_TIMEOUT_S = 8
MAX_RECORD_S = 30


chunk = bytearray(CHUNK_SIZE)
chunk_mv = memoryview(chunk)
batch = bytearray(BATCH_SIZE)
batch_mv = memoryview(batch)


def chunk_level(buf, n):
    total = 0
    count = 0
    for i in range(0, n - 1, 8):
        s = buf[i] | (buf[i + 1] << 8)
        if s & 0x8000:
            s -= 65536
        total += s if s >= 0 else -s
        count += 1
    return total // count if count else 0

led = Pin("LED", Pin.OUT)
button = Pin(10, Pin.IN, Pin.PULL_UP)

gc.enable()


def init_wifi():
    print("Connecting to WiFi...")
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    print("Available networks:")
    for net in wlan.scan():
        print(net)
    wlan.connect(SSID, PASSWORD)
    for _ in range(30):
        if wlan.isconnected():
            print("WiFi connected:", wlan.ifconfig()[0])
            led.on()
            time.sleep(0.2)
            led.off()
            return wlan
        time.sleep(0.5)
    print("WiFi failed")
    return None


def init_i2s():
    try:
        i2s = I2S(
            0,
            sck=Pin(16),
            ws=Pin(17),
            sd=Pin(18),
            mode=I2S.RX,
            bits=16,
            format=I2S.MONO,
            rate=SAMPLE_RATE,
            ibuf=IBUF_SIZE,
        )
        print("I2S initialized @ %dHz (ibuf=%d)" % (SAMPLE_RATE, IBUF_SIZE))


        junk = bytearray(4096)
        n = i2s.readinto(junk)
        print("Mic OK: read %dB" % n if n else "WARNING: mic not responding")
        return i2s
    except Exception as e:
        print("I2S init failed:", e)
        return None


def calibrate_vad(i2s):
    global SPEECH_THRESHOLD, ambient_level
    levels = []
    for _ in range(4):
        n = i2s.readinto(chunk_mv)
        if n:
            levels.append(chunk_level(chunk, n))
    if levels:
        ambient_level = sum(levels) // len(levels)
        SPEECH_THRESHOLD = max(ambient_level * VAD_MULT, VAD_FLOOR)
        print("VAD calibrated: ambient=%d threshold=%d" % (ambient_level, SPEECH_THRESHOLD))
    else:
        print("VAD calibration failed, using threshold=%d" % SPEECH_THRESHOLD)


def update_ambient(i2s):
    global SPEECH_THRESHOLD, ambient_level
    n = i2s.readinto(chunk_mv)
    if not n:
        return
    level = chunk_level(chunk, n)

    if level < ambient_level * 3:
        ambient_level = (ambient_level * 3 + level) // 4
        SPEECH_THRESHOLD = max(ambient_level * VAD_MULT, VAD_FLOOR)


def post(path, data=None, timeout=None):
    if timeout is None:
        r = urequests.post(BACKEND_URL + path, data=data)
    else:
        r = urequests.post(BACKEND_URL + path, data=data, timeout=timeout)
    return r


def run_command(name):
    try:
        mod = __import__("commands." + name)
        mod = getattr(mod, name)
        mod.run()
    except Exception as e:
        print("Command '%s' failed: %s" % (name, e))


def send_batch(filled):
    try:
        r = post("/transcribe", data=batch_mv[:filled], timeout=5)
        r.close()
        return filled
    except Exception as e:
        print("HTTP:", e)
        return 0


def record_and_send(i2s):
    if not i2s:
        print("No I2S")
        return

    try:
        post("/transcribe/reset", timeout=3).close()
    except Exception:
        pass

    total = 0
    sent = 0
    filled = 0
    heard_speech = False
    speech_ms = 0
    silence_ms = 0
    read_fail_streak = 0
    t0 = time.ticks_ms()

    print("Listening... (speak now)")

    while True:
        led.toggle()
        try:
            n = i2s.readinto(chunk_mv)
        except Exception as e:
            read_fail_streak += 1
            print("I2S read error (%d): %s" % (read_fail_streak, e))
            if read_fail_streak >= 20:
                print("I2S failing repeatedly, aborting recording.")
                break
            time.sleep_ms(10)
            continue
        if not n:
            continue
        read_fail_streak = 0
        total += n

        level = chunk_level(chunk, n)
        if level > SPEECH_THRESHOLD:
            heard_speech = True
            speech_ms += 250
            silence_ms = 0
        else:
            silence_ms += 250
        if total % BATCH_SIZE == 0:
            print("  level=%d threshold=%d %s" % (level, SPEECH_THRESHOLD,
                  "SPEAKING" if level > SPEECH_THRESHOLD else "quiet"))


        batch_mv[filled:filled + n] = chunk_mv[:n]
        filled += n
        if filled >= BATCH_SIZE:
            sent += send_batch(filled)
            filled = 0
            gc.collect()

        elapsed_s = time.ticks_diff(time.ticks_ms(), t0) // 1000

        if heard_speech and speech_ms >= MIN_SPEECH_MS and silence_ms >= SILENCE_STOP_MS:
            print("Silence detected, done.")
            break
        if not heard_speech and elapsed_s >= NO_SPEECH_TIMEOUT_S:
            print("No speech heard, giving up.")
            break
        if elapsed_s >= MAX_RECORD_S:
            print("Max duration reached.")
            break

    if filled > 0:
        sent += send_batch(filled)

    led.off()
    wall = time.ticks_diff(time.ticks_ms(), t0) / 1000
    print("Captured %dB (%.1fs audio) in %.1fs wall, sent %dB"
          % (total, total / BYTES_PER_SEC, wall, sent))

    if not heard_speech:
        try:
            post("/transcribe/reset", timeout=3).close()
        except Exception:
            pass
        return



    data = None
    try:
        try:
            import camera
            jpeg = camera.get_camera().capture()
            pr = post("/task/photo", data=jpeg, timeout=10)
            pr.close()
        except Exception as e:
            print("task photo:", e)

        try:
            r = post("/transcribe/done", timeout=30)
            data = r.json()
            r.close()
        except Exception as e:
            print("done:", e)
    finally:
        pass

    if data is None:
        return

    print("Transcript:", data.get("transcript"))

    if data.get("response"):
        try:
            import speaker
            speaker.play_url("/response/latest")
        except Exception as e:
            print("playback:", e)

    command = data.get("command", "none")
    if command and command != "none":
        print("Running command:", command)
        run_command(command)
    else:
        print("No command matched.")


def play_sfx(path):
    if not path:
        return
    try:
        import speaker
        speaker.play_file(path)
    except Exception as e:
        print("sfx (%s):" % path, e)


_thinking_stop = True
_thinking_lock = _thread.allocate_lock() if _thread else None


def _thinking_worker(path):
    global _thinking_stop
    if _thinking_lock is None:
        return
    import speaker
    _thinking_lock.acquire()
    try:
        try:
            rate, data = speaker.preload_wav(path)
        except Exception as e:
            print("thinking sfx: preload failed:", e)
            return
        speaker.play_preloaded(rate, data, stop_check=lambda: _thinking_stop)
    finally:
        _thinking_lock.release()


def start_thinking_sfx():
    global _thinking_stop
    if not _thread or _thinking_lock is None:
        print("thinking sfx: _thread not available on this build, skipping")
        return
    path = getattr(config, "SFX_THINKING", None)
    if not path:
        return
    _thinking_stop = False
    try:
        _thread.start_new_thread(_thinking_worker, (path,))
    except Exception as e:
        print("thinking sfx: failed to start:", e)
        _thinking_stop = True


def stop_thinking_sfx():
    global _thinking_stop
    if not _thread or _thinking_lock is None:
        return
    _thinking_stop = True
    _thinking_lock.acquire()
    _thinking_lock.release()


print("=== Clarity Glasses (Pico 2W) ===")
play_sfx(getattr(config, "SFX_BOOT", None))

wlan = init_wifi()
i2s = init_i2s()

if not wlan or not i2s:
    print("FATAL: WiFi or I2S failed")
    led.on()
    while True:
        time.sleep(1)

calibrate_vad(i2s)

button_pressed = False
idle_ticks = 0
print("Ready. Press button to record.")

while True:
    if not button.value() and not button_pressed:
        button_pressed = True
        play_sfx(getattr(config, "SFX_BUTTON", None))
        record_and_send(i2s)
        print("Ready for next recording.\n")
        idle_ticks = 0
        time.sleep(1)

    if button.value():
        button_pressed = False
        
    idle_ticks += 1
    if idle_ticks >= 100:
        idle_ticks = 0
        update_ambient(i2s)

    time.sleep(0.05)
