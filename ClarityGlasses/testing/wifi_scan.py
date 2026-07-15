import network

SECURITY_NAMES = {
    0: "open",
    1: "WEP",
    2: "WPA-PSK",
    3: "WPA2-PSK",
    4: "WPA/WPA2-PSK",
}


def scan_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    results = wlan.scan()

    best = {}
    for ssid, bssid, channel, rssi, security, hidden in results:
        try:
            name = ssid.decode("utf-8")
        except Exception:
            name = repr(ssid)
        if not name:
            name = "<hidden>"
        if name not in best or rssi > best[name][0]:
            best[name] = (rssi, channel, security)

    print("Found %d network(s):" % len(best))
    for name, (rssi, channel, security) in sorted(best.items(), key=lambda kv: -kv[1][0]):
        sec = SECURITY_NAMES.get(security, "security type %d" % security)
        print("  %-32s %5d dBm  ch%-3d %s" % (name, rssi, channel, sec))

    return best


if __name__ == "__main__":
    scan_wifi()
