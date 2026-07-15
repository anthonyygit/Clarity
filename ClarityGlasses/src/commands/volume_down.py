import sys

for p in ("/src", "/src/lib"):
    if p not in sys.path:
        sys.path.append(p)

import speaker
import settings

STEP = 0.5


def run():
    current = settings.get_volume()
    new_volume = speaker.set_volume(current - STEP)
    print("volume_down: %.2f -> %.2f" % (current, new_volume))
