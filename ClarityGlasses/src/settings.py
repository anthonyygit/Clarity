import json

import config

_PATH = "/src/settings.json"
_data = None


def _load():
    global _data
    if _data is not None:
        return _data
    try:
        with open(_PATH) as f:
            _data = json.load(f)
    except Exception:
        _data = {}
    return _data


def get(key, default=None):
    return _load().get(key, default)


def set(key, value):
    data = _load()
    data[key] = value
    try:
        with open(_PATH, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print("settings: save failed:", e)


def get_volume():
    return get("volume", getattr(config, "VOLUME", 1.0))
