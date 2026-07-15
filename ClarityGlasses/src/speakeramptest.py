from machine import Pin, I2S
import math
import struct

RATE = 16000
VOLUME = 12000

audio = I2S(
    1,
    sck=Pin(19),
    ws=Pin(20),
    sd=Pin(21),
    mode=I2S.TX,
    bits=16,
    format=I2S.MONO,
    rate=RATE,
    ibuf=8000,
)


def tone_buf(freq, ms=250):
    n = RATE * ms // 1000
    buf = bytearray(n * 2)
    for i in range(n):
        s = int(VOLUME * math.sin(2 * math.pi * freq * i / RATE))
        struct.pack_into("<h", buf, i * 2, s)
    return buf


print("Playing 3 tones...")
for freq in (440, 660, 880):
    print("  %d Hz" % freq)
    buf = tone_buf(freq)
    for _ in range(4):
        audio.write(buf)

audio.deinit()
print("Done. Heard three beeps? Amp works.")
