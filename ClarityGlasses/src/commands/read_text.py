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
    print("read_text: captured %d bytes" % len(jpg))

    r = urequests.post(
        BACKEND_URL + "/ocr/raw",
        data=jpg,
        headers={"Content-Type": "image/jpeg"},
    )
    data = r.json()
    r.close()
    text = data.get("text", "")
    print("read_text:", text[:200] if text else "(no text)")

    import speaker
    speaker.play_url("/response/latest")
