from ov2640_constants import *
from ov2640_lores_constants import *
from ov2640_hires_constants import *
import machine
import time
import gc


DRIVER_VERSION = "v5-reverted"


class ov2640:
    def __init__(self, spi_id=1, sck=14, mosi=15, miso=12, cs=13,
                 i2c_id=0, scl=5, sda=4, resolution=OV2640_320x240_JPEG,
                 jpeg_quality=6):
        print("ov2640: driver %s" % DRIVER_VERSION)
        self.spi = machine.SPI(
            spi_id, baudrate=4000000, polarity=0, phase=0,
            sck=machine.Pin(sck), mosi=machine.Pin(mosi), miso=machine.Pin(miso),
        )
        self.cs = machine.Pin(cs, machine.Pin.OUT, value=1)
        self.i2c = machine.I2C(i2c_id, scl=machine.Pin(scl), sda=machine.Pin(sda), freq=100000)

        addrs = self.i2c.scan()
        print("ov2640: i2c devices:", [hex(a) for a in addrs])
        if SENSORADDR not in addrs:
            raise RuntimeError("OV2640 sensor not found on I2C (check SDA/SCL wiring)")

        self.i2c.writeto_mem(SENSORADDR, 0xFF, b"\x01")
        self.i2c.writeto_mem(SENSORADDR, 0x12, b"\x80")
        time.sleep_ms(100)

        cam_write_register_set(self.i2c, SENSORADDR, OV2640_JPEG_INIT)
        cam_write_register_set(self.i2c, SENSORADDR, OV2640_YUV422)
        cam_write_register_set(self.i2c, SENSORADDR, OV2640_JPEG)

        self.i2c.writeto_mem(SENSORADDR, 0xFF, b"\x01")
        self.i2c.writeto_mem(SENSORADDR, 0x15, b"\x00")

        cam_write_register_set(self.i2c, SENSORADDR, resolution)

        self.i2c.writeto_mem(SENSORADDR, 0xFF, b"\x00")
        self.i2c.writeto_mem(SENSORADDR, 0x44, bytes([jpeg_quality]))

        self._wr(0x00, 0x55)
        if self._rd(0x00) != 0x55:
            raise RuntimeError("ArduChip SPI test failed (check SPI wiring)")
        print("ov2640: SPI OK")

        self.i2c.writeto_mem(SENSORADDR, 0xFF, b"\x01")
        pid = self.i2c.readfrom_mem(SENSORADDR, 0x0A, 1)
        ver = self.i2c.readfrom_mem(SENSORADDR, 0x0B, 1)
        print("ov2640: sensor id %s %s" % (pid.hex(), ver.hex()))

    def _wr(self, addr, val):
        self.cs.off()
        self.spi.write(bytes([addr | 0x80, val]))
        self.cs.on()

    def _rd(self, addr):
        self.cs.off()
        self.spi.write(bytes([addr & 0x7F]))
        b = self.spi.read(1)
        self.cs.on()
        return b[0]

    def _fifo_length(self):
        return ((self._rd(0x44) << 16) | (self._rd(0x43) << 8) | self._rd(0x42)) & 0x7FFFFF

    def capture_begin(self):
        self._wr(0x04, 0x01)
        self._wr(0x04, 0x02)
        time.sleep_ms(10)

        t0 = time.ticks_ms()
        while not (self._rd(0x41) & 0x08):
            if time.ticks_diff(time.ticks_ms(), t0) > 5000:
                raise RuntimeError("capture timeout")
            time.sleep_ms(10)

        length = self._fifo_length()
        if length == 0 or length >= 0x7FFFFF:
            raise RuntimeError("bad fifo length")
        return length

    def stream_fifo(self, write, length, chunk_size=2048):
        gc.collect()
        chunk = bytearray(chunk_size)
        mv = memoryview(chunk)
        self.cs.off()
        self.spi.write(b"\x3c")
        remaining = length
        try:
            while remaining > 0:
                n = min(remaining, chunk_size)
                self.spi.readinto(mv[:n])
                write(mv[:n])
                remaining -= n
        finally:
            self.cs.on()

    def capture(self):
        length = self.capture_begin()
        gc.collect()
        buf = bytearray(length)
        self.cs.off()
        self.spi.write(b"\x3c")
        self.spi.readinto(buf)
        self.cs.on()
        data = bytes(buf)
        start = data.find(b"\xff\xd8")
        end = data.rfind(b"\xff\xd9")
        if start < 0 or end < 0:
            raise RuntimeError("no JPEG markers in capture")
        return data[start:end + 2]


def cam_write_register_set(i2c, addr, regset):
    for el in regset:
        raddr = el[0]
        val = bytes([el[1]])
        if raddr == 0xFF and val == b"\xff":
            return
        i2c.writeto_mem(addr, raddr, val)
