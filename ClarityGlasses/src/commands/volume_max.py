import sys

for p in ("/src", "/src/lib"):
    if p not in sys.path:
        sys.path.append(p)

import speaker
import settings

MAX_VOLUME = 4.0


def run():
    current = settings.get_volume()
    new_volume = speaker.set_volume(MAX_VOLUME)
    print("volume_max: %.2f -> %.2f" % (current, new_volume))
