import sys

for p in ("/src", "/src/lib"):
    if p not in sys.path:
        sys.path.append(p)

import settings


def run():
    settings.set("walk_interval", "ondemand")
    print("walk_interval: ondemand")
