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
    print("describe_scene: captured %d bytes" % len(jpg))

    r = urequests.post(
        BACKEND_URL + "/scene/raw",
        data=jpg,
        headers={"Content-Type": "image/jpeg"},
    )
    data = r.json()
    r.close()
    print("describe_scene:", data.get("description", "(no description)"))

    import speaker
    speaker.play_url("/response/latest")
