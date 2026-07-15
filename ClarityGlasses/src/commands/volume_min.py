import sys

for p in ("/src", "/src/lib"):
    if p not in sys.path:
        sys.path.append(p)

import speaker
import settings

MIN_VOLUME = 0.0


def run():
    current = settings.get_volume()
    new_volume = speaker.set_volume(MIN_VOLUME)
    print("volume_min: %.2f -> %.2f" % (current, new_volume))
