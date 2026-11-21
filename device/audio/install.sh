#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
	exec sudo -n "$0" "$@"
fi

BASE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
KERNEL=$(uname -r)
AEC_PLUGIN=${AEC_PLUGIN:-"$BASE/../../outputs/audio/libspa-aec-thinksmart.so"}
ALSA_PLUGIN=${ALSA_PLUGIN:-"$BASE/../../outputs/audio/libspa-alsa-thinksmart.so"}
AMP_MODULE=${AMP_MODULE:-}
if [ -z "$AMP_MODULE" ]; then
	for candidate in "$BASE/snd-soc-tas5782m.ko" "$BASE/snd-soc-tas5782m-dbg.ko"; do
		[ -s "$candidate" ] || continue
		case "$(modinfo -F vermagic "$candidate")" in
			"$KERNEL "*) AMP_MODULE=$candidate; break ;;
		esac
	done
fi
[ -n "$AMP_MODULE" ] || { echo "缺少与运行内核 $KERNEL 匹配的 TAS5782M 模块" >&2; exit 1; }
case "$(modinfo -F vermagic "$AMP_MODULE")" in
	"$KERNEL "*) ;;
	*) echo "TAS5782M 模块与运行内核 $KERNEL 不匹配" >&2; exit 1 ;;
esac

for required in "$AEC_PLUGIN" "$ALSA_PLUGIN" "$AMP_MODULE" "$BASE/tas5728m_dsp_lenovo_cd-18781y.bin"; do
	[ -s "$required" ] || { echo "缺少音频安装文件：$required" >&2; exit 1; }
done
LICENSE_DIR="$(dirname -- "$AEC_PLUGIN")/licenses"
if ! ls "$LICENSE_DIR"/*.txt >/dev/null 2>&1; then
	echo "缺少音频插件许可证目录：$LICENSE_DIR" >&2
	exit 1
fi
THINKSMART_UID=$(id -u thinksmart)
VOICE_ACTIVE=0
systemctl is-active --quiet thinksmart-voice.service && VOICE_ACTIVE=1
restore_voice() {
	result=$?
	trap - EXIT HUP INT TERM
	if [ "$VOICE_ACTIVE" -eq 1 ]; then
		systemctl start thinksmart-voice.service || result=1
	fi
	exit "$result"
}
trap restore_voice EXIT
trap 'exit 1' HUP INT TERM
if [ "$VOICE_ACTIVE" -eq 1 ]; then systemctl stop thinksmart-voice.service; fi

install -D -m 0644 "$AEC_PLUGIN" /usr/lib/aarch64-linux-gnu/spa-0.2/aec/libspa-aec-thinksmart.so
install -D -m 0644 "$ALSA_PLUGIN" /usr/lib/aarch64-linux-gnu/spa-0.2/alsa/libspa-alsa-thinksmart.so
install -d /usr/local/share/thinksmart-aec/licenses
install -m 0644 "$LICENSE_DIR"/*.txt /usr/local/share/thinksmart-aec/licenses/

rm -f "/lib/modules/$KERNEL/extra/snd-soc-tas5782m.ko" "/lib/modules/$KERNEL/extra/snd-soc-tas5782m-dbg.ko"
install -D -m 0644 "$AMP_MODULE" "/lib/modules/$KERNEL/extra/$(basename -- "$AMP_MODULE")"
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
install -D -m 0644 "$BASE/thinksmart-alsa-pcm.conf" /etc/pipewire/pipewire.conf.d/thinksmart-alsa-pcm.conf
install -D -m 0755 "$BASE/thinksmart-volume-keys" /usr/local/bin/thinksmart-volume-keys
install -D -m 0644 "$BASE/thinksmart-volume-keys.service" /etc/systemd/system/thinksmart-volume-keys.service

install -d -o thinksmart -g thinksmart /home/thinksmart/.local /home/thinksmart/.local/state /home/thinksmart/.local/state/wireplumber
install -o thinksmart -g thinksmart -m 0644 "$BASE/default-profile" /home/thinksmart/.local/state/wireplumber/default-profile
install -o thinksmart -g thinksmart -m 0644 "$BASE/default-nodes" /home/thinksmart/.local/state/wireplumber/default-nodes

depmod -a "$KERNEL"
usermod -aG audio,pipewire thinksmart
loginctl enable-linger thinksmart
systemctl daemon-reload
systemctl enable qup-i2c-pinctrl-fix.service
systemctl restart qup-i2c-pinctrl-fix.service
systemctl start "user@${THINKSMART_UID}.service"
systemctl enable thinksmart-volume-keys.service
systemctl restart thinksmart-volume-keys.service

timeout 15 sh -c "until [ -S /run/user/$THINKSMART_UID/bus ]; do sleep 1; done"
runuser -u thinksmart -- env "XDG_RUNTIME_DIR=/run/user/$THINKSMART_UID" \
	systemctl --user restart pipewire.service wireplumber.service pipewire-pulse.service
runuser -u thinksmart -- env "XDG_RUNTIME_DIR=/run/user/$THINKSMART_UID" /usr/bin/python3 - <<'PY'
import json
import subprocess
import time

speaker = "alsa_output.platform-c051000.sound-card.HiFi__hw_cd18781y_0__sink"
mic = "alsa_input.platform-c051000.sound-card.HiFi__hw_cd18781y_1__source"
aec = "thinksmart.echo-cancel-source"


def audio_ready(objects):
    nodes = {}
    ports = {}
    links = set()
    configured = set()
    for obj in objects:
        info = obj.get("info") or {}
        props = info.get("props") or {}
        kind = obj.get("type")
        if kind == "PipeWire:Interface:Node":
            name = props.get("node.name")
            nodes[obj["id"]] = name
            if props.get("api.alsa.disable-mmap") is True:
                configured.add(name)
        elif kind == "PipeWire:Interface:Port":
            ports[obj["id"]] = (props.get("node.id"), props.get("port.name"))
        elif kind == "PipeWire:Interface:Link" and info.get("state") in ("active", "paused"):
            links.add((info["output-port-id"], info["input-port-id"]))

    names = {pid: (nodes.get(node), port) for pid, (node, port) in ports.items()}
    routes = {(names.get(src), names.get(dst)) for src, dst in links}
    return {mic, speaker} <= configured and all(
        (aec, "capture_" + ch) in names.values()
        and ((speaker, "monitor_" + ch), ("echo-cancel-sink", "input_" + ch)) in routes
        and ((mic, "capture_" + ch), ("echo-cancel-capture", "input_" + ch)) in routes
        for ch in ("FL", "FR")
    )


for attempt in range(20):
    try:
        result = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5, check=True)
        objects, _ = json.JSONDecoder().raw_decode(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        time.sleep(.5)
        continue
    if audio_ready(objects):
        break
    time.sleep(.5)
else:
    raise SystemExit("物理声卡、回声消除麦克风或其双声道连接未就绪")
PY

