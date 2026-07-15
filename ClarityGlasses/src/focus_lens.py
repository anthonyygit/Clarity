import sys
import time
import gc

for p in ("/src", "/src/lib"):
    if p not in sys.path:
        sys.path.append(p)

import socket
from config import BACKEND_URL
from ov2640 import ov2640
from ov2640_lores_constants import OV2640_640x480_JPEG

FRAME_PORT = 8002
host = BACKEND_URL.split("//")[1].rsplit(":", 1)[0]

cam = ov2640(resolution=OV2640_640x480_JPEG)


def connect():
    s = socket.socket()
    s.connect(socket.getaddrinfo(host, FRAME_PORT)[0][-1])
    print("connected to %s:%d" % (host, FRAME_PORT))
    return s


sock = connect()
frames = 0
t0 = time.ticks_ms()
print("\nStreaming — watch the browser. Ctrl+C when focused.\n")

while True:
    try:
        length = cam.capture_begin()
        sock.write(length.to_bytes(4, "big"))
        cam.stream_fifo(sock.write, length)
        frames += 1
        if frames % 20 == 0:
            elapsed = time.ticks_diff(time.ticks_ms(), t0) / 1000
            print("%.1f fps" % (frames / elapsed))
            gc.collect()
    except Exception as e:
        print("error, reconnecting:", e)
        try:
            sock.close()
        except Exception:
            pass
        time.sleep_ms(500)
        while True:
            try:
                sock = connect()
                break
            except Exception as e2:
                print("reconnect failed:", e2)
                time.sleep_ms(1000)
