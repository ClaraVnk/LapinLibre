#!/bin/sh
# SPDX-License-Identifier: MIT
# The setup network, for startup.sh when the rabbit has no Wi-Fi settings or can't join its
# network:   hotspot.sh [SECONDS]   (how long it stays open, default 600)
# The rabbit becomes the access point "Karotz-XXXX" (the last four hex digits of its MAC): WPA2,
# with an eight-digit passphrase drawn at random each time and said out loud twice, never an open
# network that anyone passing by could use to set the rabbit up. The nose pulses purple. A phone
# that joins gets an address (udhcpd) and the setup page, http://192.168.75.1/setup.html, which
# hands the Wi-Fi settings over (cgi-bin/wifi_setup, as the user web, into $RUN/in); this checks
# them again, writes them as the key's install does and reboots the rabbit.
# Exits 0 once settings are saved (the reboot follows), 1 when nobody set it up in time or the
# radio refused to be an access point; the network is then left as it was found, down.
PATH=/bin:/sbin:/usr/bin:/usr/sbin:/karotz/bin:/usr/karotz/bin
export PATH
RUN=/var/run/hotspot
LIMIT=${1:-600}
ADDR=192.168.75.1
SOUNDS=/usr/karotz/sounds/digits
LED=/karotz/bin/led

log() {
    echo "hotspot: $*"
    logger -t hotspot "$*" 2> /dev/null
}

stop() {
    [ -f $RUN/udhcpd.pid ] && kill "$(cat $RUN/udhcpd.pid)" 2> /dev/null
    [ -f $RUN/wpa.pid ] && kill "$(cat $RUN/wpa.pid)" 2> /dev/null
    ifconfig wlan0 0.0.0.0 2> /dev/null
    rm -rf $RUN/active $RUN/wpa.conf $RUN/udhcpd.conf $RUN/in
}

# say PASS: the digits one by one, in the rabbit's language when there are sounds for it, else in
# English; nothing when the image has no digit sounds (the page and the LED still work).
say() {
    lang=$(sed -n 's/^lang=\([a-z][a-z]\).*/\1/p' /usr/etc/conf/karotz.conf 2> /dev/null | head -n 1)
    [ -f "$SOUNDS/${lang:-en}_0.mp3" ] || lang=en
    [ -f "$SOUNDS/${lang}_0.mp3" ] || { log "no digit sounds in $SOUNDS"; return 0; }
    rest=$1
    while [ -n "$rest" ]; do
        d=${rest%"${rest#?}"}
        rest=${rest#?}
        karotz-player "$SOUNDS/${lang}_$d.mp3" > /dev/null 2>&1
    done
}

# take: the settings the page handed over, checked again (the page's own rules), into
# /usr/etc/conf as applysettings.sh writes them (WPA2 and WPA3, an address from the router).
take() {
    f=$RUN/in/settings
    [ -s $f ] || return 1
    ssid=$(sed -n 1p $f)
    psk=$(sed -n 2p $f)
    rm -f $f
    [ ${#ssid} -ge 1 ] && [ ${#ssid} -le 32 ] && [ ${#psk} -ge 8 ] && [ ${#psk} -le 63 ] || return 1
    case "$ssid$psk" in *[\"\\]* | *[![:print:]]*) return 1 ;; esac
    (umask 077 && printf 'network={\nssid="%s"\npsk="%s"\nkey_mgmt=WPA-PSK SAE\nieee80211w=1\nscan_ssid=1\nproto=WPA RSN\n}\n' \
        "$ssid" "$psk" > /usr/etc/conf/wpa.conf.new) && mv -f /usr/etc/conf/wpa.conf.new /usr/etc/conf/wpa.conf || return 1
    printf 'IP=""\nNETMASK=""\nGATEWAY=""\nDNS=""\n' > /usr/etc/conf/network.conf
    log "Wi-Fi network $ssid saved"
    : > $RUN/saved
}

mac=$(tr -d ':' < /sys/class/net/wlan0/address 2> /dev/null)
[ ${#mac} -eq 12 ] || { log "no wlan0"; exit 1; }
suffix=$(printf '%s' "${mac#????????}" | tr a-f A-F)
# Eight digits from /dev/urandom (seeded by startup.sh): one byte each, the bytes 250 and up
# dropped so that every digit is as likely.
pass=$(od -An -v -tu1 -N64 /dev/urandom | tr -s ' ' '\n' | awk '$1 != "" && $1 < 250 { printf "%d", $1 % 10; if (++n == 8) exit }')
[ ${#pass} -eq 8 ] || { log "no random digits"; exit 1; }

mkdir -p $RUN
(
    umask 077
    printf 'ap_scan=2\nnetwork={\nssid="Karotz-%s"\nmode=2\nfrequency=2437\nkey_mgmt=WPA-PSK\nproto=RSN\npairwise=CCMP\ngroup=CCMP\npsk="%s"\n}\n' \
        "$suffix" "$pass" > $RUN/wpa.conf
    printf 'start 192.168.75.10\nend 192.168.75.40\ninterface wlan0\nmax_leases 8\nlease_file %s/leases\npidfile %s/udhcpd.pid\nopt subnet 255.255.255.0\nopt lease 600\n' \
        "$RUN" "$RUN" > $RUN/udhcpd.conf
)
rm -f $RUN/saved
rm -rf $RUN/in
mkdir -p $RUN/in && chown web $RUN/in && chmod 700 $RUN/in || { log "no drop folder for the web user"; exit 1; }
: > $RUN/leases
killall wpa_supplicant udhcpc 2> /dev/null
sleep 1
ifconfig wlan0 "$ADDR" netmask 255.255.255.0 up
if ! wpa_supplicant -iwlan0 -Dnl80211 -B -P$RUN/wpa.pid -c$RUN/wpa.conf; then
    log "the radio refused to be an access point"
    stop
    exit 1
fi
udhcpd $RUN/udhcpd.conf
: > $RUN/active
log "Karotz-$suffix open for ${LIMIT} s: http://$ADDR/setup.html"
killall led 2> /dev/null
$LED -l 8000FF -p 000000 -d 700 &
say "$pass"
sleep 2
say "$pass"

i=0
while [ $i -lt "$LIMIT" ] && [ ! -f $RUN/saved ]; do
    take
    sleep 2
    i=$((i + 2))
done
killall led 2> /dev/null
if [ -f $RUN/saved ]; then
    log "settings saved: rebooting"
    sleep 3
    stop
    setsid reboot < /dev/null > /dev/null 2>&1 &
    exit 0
fi
log "nobody set it up in ${LIMIT} s: closed"
stop
exit 1
