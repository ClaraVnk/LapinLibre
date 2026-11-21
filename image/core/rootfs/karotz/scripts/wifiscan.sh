#!/bin/sh
# SPDX-License-Identifier: MIT
# The Wi-Fi networks around, for the setup network's page (hotspot.sh runs it before the radio
# becomes the access point, which can't scan): one line per network name, the strongest first,
#   <quality 0-100>	<WPA3|WPA2|WPA|open>	<name>
# Names the page can't take (a quote, a backslash, a control character, over 32 bytes) and hidden
# ones are left out: they are typed. A Naboot rabbit's page lists them; this one had only a field
# to type the name in (Clara, 2026-11-21).
#   wifiscan.sh [iwlist output file]    # a file instead of the radio, for the tests
if [ -n "$1" ]; then cat "$1"; else iwlist wlan0 scan 2> /dev/null; fi | awk '
function flush() {
    if (ssid != "" && length(ssid) <= 32 && ssid !~ /["\\]/ && ssid !~ /[^[:print:]]/) {
        sec = enc ? (sae ? "WPA3" : (rsn ? "WPA2" : (wpa ? "WPA" : "WPA2"))) : "open"
        if (!(ssid in best) || q > best[ssid]) { best[ssid] = q; kind[ssid] = sec }
    }
    ssid = ""; q = 0; enc = 0; rsn = 0; wpa = 0; sae = 0
}
/Cell [0-9]+ - Address/ { flush(); next }
/ESSID:"/ { s = $0; sub(/.*ESSID:"/, "", s); sub(/"[[:space:]]*$/, "", s); ssid = s; next }
/Quality[=:]/ {
    s = $0; sub(/.*Quality[=:]/, "", s); split(s, a, /[\/ ]/)
    if (a[2] + 0 > 0) q = int(a[1] * 100 / a[2]); else q = a[1] + 0
    next
}
/Encryption key:on/ { enc = 1; next }
/IEEE 802\.11i\/WPA2/ { rsn = 1; next }
/WPA Version/ { wpa = 1; next }
/Authentication Suites.*(SAE|8)/ { sae = 1; next }
END {
    flush()
    for (n in best) printf "%d\t%s\t%s\n", best[n], kind[n], n
}' | sort -t "$(printf '\t')" -k1,1nr
