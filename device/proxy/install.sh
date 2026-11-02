#!/bin/sh
set -eu
BASE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PAYLOAD=${1:?Path to the exported proxy directory is required}
ROOT=/data/thinksmart/proxy
if [ "$(id -u)" -ne 0 ]; then
    exec sudo -n "$0" "$PAYLOAD"
fi
umask 077
install -d -m 0700 "$ROOT" "$ROOT/bin"
install -m 0755 "$PAYLOAD/mihomo" "$ROOT/bin/mihomo.new"
mv -f "$ROOT/bin/mihomo.new" "$ROOT/bin/mihomo"
cp -R "$PAYLOAD/ruleset" "$ROOT/"
for file in Country.mmdb ASN.mmdb geoip.dat geosite.dat controller.json selections.json; do
    install -m 0600 "$PAYLOAD/$file" "$ROOT/$file"
done
install -m 0600 "$PAYLOAD/bootstrap.yaml" "$ROOT/config.yaml"
install -m 0600 "$PAYLOAD/config.yaml" "$ROOT/tun.yaml"
install -m 0644 "$BASE/mihomo.service" /etc/systemd/system/mihomo.service
install -m 0755 "$BASE/select.py" /usr/local/sbin/thinksmart-proxy-select
ln -sfn "$ROOT/bin/mihomo" /usr/local/bin/mihomo
"$ROOT/bin/mihomo" -t -d "$ROOT" -f "$ROOT/config.yaml"
systemctl daemon-reload
systemctl restart mihomo.service
