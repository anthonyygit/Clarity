import network
wlan = network.WLAN(network.STA_IF)
wlan.active(True)
print("Available networks:")
for net in wlan.scan():
    print(net)