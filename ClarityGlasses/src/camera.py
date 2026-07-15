import sys

for p in ("/src", "/src/lib"):
    if p not in sys.path:
        sys.path.append(p)

_cam = None


def get_camera():
    global _cam
    if _cam is None:
        from ov2640 import ov2640
        from ov2640_hires_constants import OV2640_1024x768_JPEG
        print("camera: initializing...")
        _cam = ov2640(resolution=OV2640_1024x768_JPEG)
    return _cam
