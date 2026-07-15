import sys
import gc
import urequests

for p in ("/src", "/src/lib"):
    if p not in sys.path:
        sys.path.append(p)

from config import BACKEND_URL
from camera import get_camera


def run():
    cam = get_camera()
    gc.collect()
    jpg = cam.capture()
    print("take_photo: captured %d bytes" % len(jpg))

    r = urequests.post(
        BACKEND_URL + "/photo",
        data=jpg,
        headers={"Content-Type": "image/jpeg"},
    )
    print("take_photo: uploaded ->", r.json())
    r.close()
