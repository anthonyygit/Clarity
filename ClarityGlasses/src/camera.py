import sys

for p in ("/src", "/src/lib"):
    if p not in sys.path:
        sys.path.append(p)

_cam = None
_current_res = None


def get_camera():
    global _cam, _current_res
    if _cam is None:
        from ov2640 import ov2640
        from ov2640_hires_constants import OV2640_1024x768_JPEG
        print("camera: initializing...")
        _cam = ov2640(resolution=OV2640_1024x768_JPEG)
        _current_res = "hires"
    return _cam


def get_camera_hires():
    """Full 1024x768 capture, for deliberate one-off commands where detail
    matters (describe_scene, read_text, take_photo)."""
    global _current_res
    cam = get_camera()
    if _current_res != "hires":
        from ov2640_hires_constants import OV2640_1024x768_JPEG
        cam.set_resolution(OV2640_1024x768_JPEG)
        _current_res = "hires"
    return cam


def get_camera_fast():
    """Small 320x240 capture, for walking mode's frequent ticks — cuts
    capture time (SPI FIFO read), upload time, and Claude's image-processing
    time versus the full-res capture. A quick glance doesn't need detail,
    it needs speed."""
    global _current_res
    cam = get_camera()
    if _current_res != "lores":
        from ov2640_lores_constants import OV2640_320x240_JPEG
        cam.set_resolution(OV2640_320x240_JPEG)
        _current_res = "lores"
    return cam
