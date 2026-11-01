#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
	exec sudo -n "$0" "$@"
fi

BASE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
KERNEL=$(uname -r)
AEC_PLUGIN=${AEC_PLUGIN:-"$BASE/../../outputs/audio/libspa-aec-thinksmart.so"}

install -D -m 0644 "$AEC_PLUGIN" /usr/lib/aarch64-linux-gnu/spa-0.2/aec/libspa-aec-thinksmart.so
install -d /usr/local/share/thinksmart-aec/licenses
install -m 0644 "$(dirname -- "$AEC_PLUGIN")"/licenses/*.txt /usr/local/share/thinksmart-aec/licenses/

install -D -m 0644 "$BASE/snd-soc-tas5782m.ko" "/lib/modules/$KERNEL/extra/snd-soc-tas5782m.ko"
install -D -m 0644 "$BASE/tas5728m_dsp_lenovo_cd-18781y.bin" /lib/firmware/tas5728m_dsp_lenovo_cd-18781y.bin
ln -sfn tas5728m_dsp_lenovo_cd-18781y.bin /lib/firmware/tas5728m_dsp_default.bin

install -D -m 0644 "$BASE/audio-cd18781y.conf" /etc/modules-load.d/audio-cd18781y.conf
install -D -m 0755 "$BASE/qup-i2c-pinctrl-fix.sh" /usr/local/sbin/qup-i2c-pinctrl-fix.sh
install -D -m 0644 "$BASE/qup-i2c-pinctrl-fix.service" /etc/systemd/system/qup-i2c-pinctrl-fix.service

install -D -m 0644 "$BASE/ucm/cd-18781y.conf" /usr/share/alsa/ucm2/conf.d/cd-18781y/cd-18781y.conf
install -D -m 0644 "$BASE/ucm/cd18781y.conf" /usr/share/alsa/ucm2/conf.d/cd18781y/cd18781y.conf
install -D -m 0644 "$BASE/ucm/HiFi.conf" /usr/share/alsa/ucm2/Lenovo/cd-18781y/HiFi.conf
install -D -m 0644 "$BASE/ucm/HiFi.conf" /usr/share/alsa/ucm2/Qualcomm/cd-18781y/HiFi.conf
install -D -m 0644 "$BASE/ucm/qualcomm-card.conf" /usr/share/alsa/ucm2/Qualcomm/cd-18781y/cd-18781y.conf
install -D -m 0644 "$BASE/99-cd18781y-alsa.lua" /etc/wireplumber/main.lua.d/99-cd18781y-alsa.lua
install -D -m 0644 "$BASE/thinksmart-echo-cancel.conf" /etc/pipewire/pipewire.conf.d/thinksmart-echo-cancel.conf
install -D -m 0755 "$BASE/thinksmart-volume-keys" /usr/local/bin/thinksmart-volume-keys
install -D -m 0644 "$BASE/thinksmart-volume-keys.service" /etc/systemd/system/thinksmart-volume-keys.service

install -d -o thinksmart -g thinksmart /home/thinksmart/.local/state/wireplumber
install -o thinksmart -g thinksmart -m 0644 "$BASE/default-profile" /home/thinksmart/.local/state/wireplumber/default-profile
install -o thinksmart -g thinksmart -m 0644 "$BASE/default-nodes" /home/thinksmart/.local/state/wireplumber/default-nodes

depmod -a "$KERNEL"
usermod -aG audio,pipewire thinksmart
loginctl enable-linger thinksmart
systemctl daemon-reload
systemctl enable qup-i2c-pinctrl-fix.service
systemctl start qup-i2c-pinctrl-fix.service
modprobe snd_soc_tas5782m
systemctl start user@1000.service
systemctl enable --now thinksmart-volume-keys.service

