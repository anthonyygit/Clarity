import sys

for p in ("/src", "/src/lib"):
    if p not in sys.path:
        sys.path.append(p)

from machine import Pin, I2S
import micropython
import time
import urequests
import config
import settings
from config import BACKEND_URL

_audio = None
_button = Pin(10, Pin.IN, Pin.PULL_UP)


def _vol_fixed_point(volume):
    return min(max(int(volume * 256), 0), 1024)


_VOL = _vol_fixed_point(settings.get_volume())


def set_volume(volume):
    global _VOL
    volume = min(max(volume, 0.0), 4.0)
    _VOL = _vol_fixed_point(volume)
    settings.set("volume", volume)
    return volume


@micropython.viper
def _scale(buf: ptr16, n: int, mul: int):
    for i in range(n):
        v = int(buf[i])
        if v & 0x8000:
            v -= 0x10000
        v = (v * mul) >> 8
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        buf[i] = v & 0xFFFF


def get_speaker(rate=16000):
    global _audio
    if _audio is None or _audio_rate[0] != rate:
        if _audio is not None:
            try:
                _audio.deinit()
            except Exception:
                pass
        _audio = I2S(
            1,
            sck=Pin(19),
            ws=Pin(20),
            sd=Pin(21),
            mode=I2S.TX,
            bits=16,
            format=I2S.MONO,
            rate=rate,
            ibuf=8000,
        )
        _audio_rate[0] = rate
    return _audio


_audio_rate = [16000]


def _wav_rate(f):
    header = f.read(44)
    if len(header) < 44 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise ValueError("not a valid WAV file")
    channels = int.from_bytes(header[22:24], "little")
    rate = int.from_bytes(header[24:28], "little")
    if channels != 1:
        raise ValueError(
            "WAV is %d-channel, need mono (convert with: ffmpeg -i in.wav -ac 1 out.wav)"
            % channels
        )
    return rate


def _shutup():
    global _audio
    print("speaker: interrupted")
    try:
        _audio.deinit()
    except Exception:
        pass
    _audio = None
    while not _button.value():
        time.sleep_ms(20)
    time.sleep_ms(100)


def play_response(r, interruptible=False):
    """Play audio straight from an already-open urequests response (e.g. a
    POST whose body is streamed PCM), instead of a separate GET to fetch
    it afterward. Does not close r — caller owns that."""
    audio = get_speaker(16000)
    try:
        raw = r.raw
        buf = bytearray(2048)
        mv = memoryview(buf)
        while True:
            if interruptible and not _button.value():
                _shutup()
                return False
            n = raw.readinto(mv)
            if not n:
                break
            if _VOL != 256:
                _scale(buf, n // 2, _VOL)
            audio.write(mv[:n])
        return True
    except Exception as e:
        print("speaker response error:", e)
        return False


def play_url(path="/response/latest"):
    audio = get_speaker(16000)
    r = None
    try:
        r = urequests.get(BACKEND_URL + path)
        if r.status_code != 200:
            print("speaker: no audio (%d)" % r.status_code)
            return False
        raw = r.raw
        buf = bytearray(2048)
        mv = memoryview(buf)
        while True:
            if not _button.value():
                _shutup()
                return False
            n = raw.readinto(mv)
            if not n:
                break
            if _VOL != 256:
                _scale(buf, n // 2, _VOL)
            audio.write(mv[:n])
        return True
    except Exception as e:
        print("speaker error:", e)
        return False
    finally:
        if r:
            try:
                r.close()
            except Exception:
                pass


def play_file(path, interruptible=False):
    try:
        with open(path, "rb") as f:
            rate = _wav_rate(f)
            audio = get_speaker(rate)
            buf = bytearray(2048)
            mv = memoryview(buf)
            while True:
                if interruptible and not _button.value():
                    _shutup()
                    return False
                n = f.readinto(buf)
                if not n:
                    break
                if _VOL != 256:
                    _scale(buf, n // 2, _VOL)
                audio.write(mv[:n])
        return True
    except Exception as e:
        print("speaker file error:", e)
        return False


def preload_wav(path):
    with open(path, "rb") as f:
        rate = _wav_rate(f)
        data = bytearray(f.read())
    if _VOL != 256:
        _scale(data, len(data) // 2, _VOL)
    return rate, data


def play_preloaded(rate, data, interruptible=False, stop_check=None):
    audio = get_speaker(rate)
    mv = memoryview(data)
    n = len(data)
    pos = 0
    while pos < n:
        if interruptible and not _button.value():
            _shutup()
            return False
        if stop_check and stop_check():
            return False
        end = pos + 2048
        if end > n:
            end = n
        audio.write(mv[pos:end])
        pos = end
    return True
