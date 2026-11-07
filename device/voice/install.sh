#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
	exec sudo -n "$0" "$@"
fi

BASE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APP_ROOT=/usr/local/lib/thinksmart-voice
DATA_ROOT=/data/thinksmart/voice
VENV="$DATA_ROOT/venv"
MODEL_ROOT="$DATA_ROOT/models"
MODEL_NAME=sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20
MODEL_DIR="$MODEL_ROOT/$MODEL_NAME"
LEGACY_DATA_MODEL="/data/thinksmart/models/kws/$MODEL_NAME"
LEGACY_OPT_MODEL="/opt/thinksmart-voice/models/$MODEL_NAME"
CACHE_ROOT=/var/cache/thinksmart-voice
MODEL_ARCHIVE="$CACHE_ROOT/$MODEL_NAME.tar.bz2"
MODEL_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/$MODEL_NAME.tar.bz2"

UI_SRC="$BASE/../ui"
UI_ROOT=/usr/local/lib/thinksmart-ui
UI_DATA=/data/thinksmart/ui
UI_MODULES="app.py fbdev.py ime.py system.py touch.py"
CLOCK_SRC="$BASE/../clock"
AUDIO_SRC="$BASE/../audio"
GATEWAY_SRC="$BASE/../gateway"
GATEWAY_ROOT=/usr/local/lib/thinksmart-gateway
GATEWAY_UNITS="thinksmart-gateway.service thinksmart-gateway-maintenance.path thinksmart-gateway-maintenance.service"
for file in app.py helper.py schema.py web/index.html web/app.js web/style.css $GATEWAY_UNITS; do
    if [ ! -f "$GATEWAY_SRC/$file" ]; then
        echo "缺少管理网关文件：$GATEWAY_SRC/$file" >&2
        exit 1
    fi
done

for file in thinksmart-volume-keys thinksmart-volume-keys.service; do
    if [ ! -f "$AUDIO_SRC/$file" ]; then
        echo "缺少硬件输入服务：$AUDIO_SRC/$file" >&2
        exit 1
    fi
done

for module in $UI_MODULES; do
	if [ ! -f "$UI_SRC/$module" ]; then
		echo "缺少界面源文件：$UI_SRC/$module" >&2
		exit 1
	fi
done
for file in thinksmart-clock.py thinksmart-clock-restore.service thinksmart-clock-save.service thinksmart-clock-save.timer; do
	if [ ! -f "$CLOCK_SRC/$file" ]; then
		echo "缺少时间服务源文件：$CLOCK_SRC/$file" >&2
		exit 1
	fi
done

install -d -m 0755 "$DATA_ROOT" "$MODEL_ROOT" "$CACHE_ROOT"
STAGE=$(mktemp -d "$DATA_ROOT/install-XXXXXXXX")
chmod 0755 "$STAGE"
COMMIT=0
VENV_SWAPPED=0
VOICE_ACTIVE=0
UI_ACTIVE=0
WAKE_ACTIVE=0
KEYS_ACTIVE=0
GATEWAY_ACTIVE=0
MAINTENANCE_ACTIVE=0
systemctl is-active --quiet thinksmart-voice.service && VOICE_ACTIVE=1
systemctl is-active --quiet thinksmart-ui.service && UI_ACTIVE=1
systemctl is-active --quiet thinksmart-wake-request.path && WAKE_ACTIVE=1
systemctl is-active --quiet thinksmart-volume-keys.service && KEYS_ACTIVE=1
systemctl is-active --quiet thinksmart-gateway.service && GATEWAY_ACTIVE=1
systemctl is-active --quiet thinksmart-gateway-maintenance.path && MAINTENANCE_ACTIVE=1
TARGETS="$APP_ROOT $UI_ROOT /usr/local/lib/thinksmart-clock
/etc/thinksmart-voice.conf /etc/thinksmart-ui.conf
/etc/thinksmart-voice/wake-phrase $DATA_ROOT/keywords.txt $DATA_ROOT/offline-keywords.txt
/usr/local/sbin/thinksmart-configure-voice /usr/local/sbin/thinksmart-configure-chat
/usr/local/sbin/thinksmart-configure-expert /usr/local/sbin/thinksmart-set-wake-word
/etc/systemd/system/thinksmart-voice.service /etc/systemd/system/thinksmart-ui.service
/etc/systemd/system/thinksmart-wake-request.path /etc/systemd/system/thinksmart-wake-request.service
/etc/systemd/system/thinksmart-clock-restore.service /etc/systemd/system/thinksmart-clock-save.service
/etc/systemd/system/thinksmart-clock-save.timer /etc/udev/rules.d/70-thinksmart-backlight.rules"
TARGETS="$TARGETS /usr/local/bin/thinksmart-volume-keys /etc/systemd/system/thinksmart-volume-keys.service"
TARGETS="$TARGETS $GATEWAY_ROOT"
for unit in $GATEWAY_UNITS; do TARGETS="$TARGETS /etc/systemd/system/$unit"; done
rollback() {
    result=$?
    trap - EXIT HUP INT TERM
    if [ "$result" -ne 0 ] && [ "$COMMIT" -eq 1 ]; then
        echo '安装失败，正在恢复原程序和服务。' >&2
        systemctl stop thinksmart-gateway.service thinksmart-gateway-maintenance.path thinksmart-gateway-maintenance.service || true
        systemctl stop thinksmart-wake-request.path thinksmart-ui.service thinksmart-voice.service thinksmart-volume-keys.service || true
        for target in $TARGETS; do
            rm -rf -- "$target"
            if [ -e "$STAGE/backup$target" ] || [ -L "$STAGE/backup$target" ]; then
                mkdir -p "$(dirname -- "$target")"
                cp -a "$STAGE/backup$target" "$target"
            fi
        done
        if [ "$VENV_SWAPPED" -eq 1 ]; then
            rm -f "$VENV"
            if [ -e "$STAGE/previous-venv" ] || [ -L "$STAGE/previous-venv" ]; then
                mv "$STAGE/previous-venv" "$VENV"
            fi
        fi
        systemctl daemon-reload || true
        [ "$KEYS_ACTIVE" -ne 1 ] || systemctl start thinksmart-volume-keys.service || true
        [ "$VOICE_ACTIVE" -ne 1 ] || systemctl start thinksmart-voice.service || true
        [ "$UI_ACTIVE" -ne 1 ] || systemctl start thinksmart-ui.service || true
        [ "$WAKE_ACTIVE" -ne 1 ] || systemctl start thinksmart-wake-request.path || true
        [ "$MAINTENANCE_ACTIVE" -ne 1 ] || systemctl start thinksmart-gateway-maintenance.path || true
        [ "$GATEWAY_ACTIVE" -ne 1 ] || systemctl start thinksmart-gateway.service || true
    fi
    if [ "$result" -ne 0 ]; then
        rm -rf "$STAGE"
    fi
    exit "$result"
}
trap rollback EXIT
trap 'exit 1' HUP INT TERM

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
	python3-venv curl ca-certificates bzip2
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
	python3-pil fonts-wqy-microhei fonts-dejavu-core fonts-dejavu-extra libmpg123-0t64 libpulse0 pipewire-pulse espeak-ng
runuser -u thinksmart -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user enable --now pipewire-pulse.socket

# All network-dependent preparation finishes while the old services still run.
python3 -m venv "$STAGE/venv"
if [ -x "$VENV/bin/pip" ]; then
    "$VENV/bin/pip" freeze > "$STAGE/constraints.txt"
else
    : > "$STAGE/constraints.txt"
fi
"$STAGE/venv/bin/pip" install --no-cache-dir \
    --constraint "$STAGE/constraints.txt" \
	"dashscope>=2.0.1,<3" \
	"sherpa-onnx==2.0.1" \
	"sentencepiece>=0.2,<1" \
	"pypinyin>=0.55,<1" \
	"aiohttp>=3.12,<4" \
	"qqmusic-api-python==0.8.2"

if [ ! -f "$MODEL_DIR/tokens.txt" ]; then
	if [ -f "$LEGACY_DATA_MODEL/tokens.txt" ]; then
		ln -s "$LEGACY_DATA_MODEL" "$MODEL_DIR"
	elif [ -f "$LEGACY_OPT_MODEL/tokens.txt" ]; then
		mv "$LEGACY_OPT_MODEL" "$MODEL_DIR"
	else
		if [ ! -s "$MODEL_ARCHIVE" ]; then
			curl -fL --retry 3 -o "$MODEL_ARCHIVE.part" "$MODEL_URL"
			mv "$MODEL_ARCHIVE.part" "$MODEL_ARCHIVE"
		fi
		tar -xjf "$MODEL_ARCHIVE" -C "$MODEL_ROOT"
	fi
fi

"$STAGE/venv/bin/sherpa-onnx-cli" text2token \
	--tokens "$MODEL_DIR/tokens.txt" --tokens-type ppinyin \
	"$BASE/offline-keywords.txt" "$STAGE/offline-keywords.txt"

mkdir -p "$STAGE/backup"
chmod 0700 "$STAGE/backup"
for target in $TARGETS; do
    if [ -e "$target" ] || [ -L "$target" ]; then
        cp -a --parents "$target" "$STAGE/backup/"
    fi
done
COMMIT=1
if systemctl is-active --quiet thinksmart-gateway-maintenance.service; then
    COMMIT=0
    echo '设备维护操作尚未结束，请稍后重新安装。' >&2
    exit 1
fi
systemctl stop thinksmart-gateway-maintenance.path thinksmart-gateway.service >/dev/null 2>&1 || true
systemctl stop thinksmart-wake-request.path >/dev/null 2>&1 || true
for unit in thinksmart-ui.service thinksmart-voice.service; do
    if systemctl cat "$unit" >/dev/null 2>&1; then
        systemctl stop "$unit"
    fi
done
if [ -e "$VENV" ] || [ -L "$VENV" ]; then
    mv "$VENV" "$STAGE/previous-venv"
fi
VENV_SWAPPED=1
ln -s "$STAGE/venv" "$VENV"
install -m 0644 "$STAGE/offline-keywords.txt" "$DATA_ROOT/offline-keywords.txt"

install -d -m 0755 "$APP_ROOT" "$DATA_ROOT" "$MODEL_ROOT" "$CACHE_ROOT"
install -d -m 0755 /etc/thinksmart-voice
install -m 0755 "$BASE/assistant.py" "$APP_ROOT/assistant.py"
install -m 0644 "$BASE/music.py" "$APP_ROOT/music.py"
install -m 0644 "$BASE/alarms.py" "$APP_ROOT/alarms.py"
install -m 0644 "$BASE/offline.py" "$BASE/information.py" "$BASE/brain.py" "$BASE/playback.py" "$APP_ROOT/"
install -d -o thinksmart -g thinksmart -m 0700 /data/thinksmart/music
install -m 0755 "$BASE/configure_voice.py" /usr/local/sbin/thinksmart-configure-voice
install -m 0755 "$BASE/configure_chat.py" /usr/local/sbin/thinksmart-configure-chat
install -m 0755 "$BASE/configure_expert.py" /usr/local/sbin/thinksmart-configure-expert
install -m 0755 "$BASE/set-wake-word.sh" /usr/local/sbin/thinksmart-set-wake-word
install -m 0644 "$BASE/thinksmart-voice.service" /etc/systemd/system/thinksmart-voice.service
install -d -m 0755 "$GATEWAY_ROOT" "$GATEWAY_ROOT/web"
install -m 0644 "$GATEWAY_SRC/app.py" "$GATEWAY_SRC/helper.py" "$GATEWAY_SRC/schema.py" "$GATEWAY_ROOT/"
install -m 0644 "$GATEWAY_SRC/web/index.html" "$GATEWAY_SRC/web/app.js" "$GATEWAY_SRC/web/style.css" "$GATEWAY_ROOT/web/"
for unit in $GATEWAY_UNITS; do install -m 0644 "$GATEWAY_SRC/$unit" "/etc/systemd/system/$unit"; done
install -m 0755 "$AUDIO_SRC/thinksmart-volume-keys" /usr/local/bin/thinksmart-volume-keys
install -m 0644 "$AUDIO_SRC/thinksmart-volume-keys.service" /etc/systemd/system/thinksmart-volume-keys.service
if [ ! -f /etc/thinksmart-voice.conf ]; then
	install -m 0644 "$BASE/thinksmart-voice.conf" /etc/thinksmart-voice.conf
fi

install -d -m 0755 /usr/local/lib/thinksmart-clock
install -m 0644 "$CLOCK_SRC/thinksmart-clock.py" /usr/local/lib/thinksmart-clock/thinksmart-clock.py
for unit in thinksmart-clock-restore.service thinksmart-clock-save.service thinksmart-clock-save.timer; do
	install -m 0644 "$CLOCK_SRC/$unit" "/etc/systemd/system/$unit"
done

install -d -m 0755 "$UI_ROOT"
for module in $UI_MODULES; do
	install -m 0644 "$UI_SRC/$module" "$UI_ROOT/$module"
done
install -m 0644 "$UI_SRC/thinksmart-ui.service" /etc/systemd/system/thinksmart-ui.service
install -m 0644 "$UI_SRC/thinksmart-wake-request.path" /etc/systemd/system/thinksmart-wake-request.path
install -m 0644 "$UI_SRC/thinksmart-wake-request.service" /etc/systemd/system/thinksmart-wake-request.service
install -m 0644 "$UI_SRC/70-thinksmart-backlight.rules" /etc/udev/rules.d/70-thinksmart-backlight.rules
if [ ! -f /etc/thinksmart-ui.conf ]; then
	install -m 0644 "$UI_SRC/thinksmart-ui.conf" /etc/thinksmart-ui.conf
fi
install -d -o thinksmart -g thinksmart -m 0700 "$UI_DATA"

if [ ! -s /etc/thinksmart-voice/wake-phrase ]; then
	printf '%s\n' '小爱同学' > /etc/thinksmart-voice/wake-phrase
fi
/usr/local/sbin/thinksmart-set-wake-word --stdin < /etc/thinksmart-voice/wake-phrase

chown thinksmart:thinksmart "$DATA_ROOT" "$DATA_ROOT/offline-keywords.txt"
chmod 0644 /etc/thinksmart-voice/wake-phrase "$DATA_ROOT/keywords.txt"
systemctl daemon-reload
systemctl enable thinksmart-volume-keys.service
systemctl restart thinksmart-volume-keys.service
systemctl enable thinksmart-clock-restore.service
systemctl enable --now thinksmart-clock-save.timer
systemctl start thinksmart-clock-save.service

if [ -f /etc/thinksmart-voice/aliyun.json ]; then
	chmod 0600 /etc/thinksmart-voice/aliyun.json
	chown root:root /etc/thinksmart-voice/aliyun.json
	systemctl enable --now thinksmart-voice.service
else
	systemctl disable thinksmart-voice.service >/dev/null 2>&1 || true
	printf '%s\n' '语音系统已安装，等待配置阿里云百炼凭据。'
fi

udevadm control --reload-rules >/dev/null 2>&1 || true
udevadm trigger --subsystem-match=backlight --action=add >/dev/null 2>&1 || true
systemctl disable getty@tty1.service >/dev/null 2>&1 || true
systemctl enable --now thinksmart-wake-request.path
systemctl enable thinksmart-ui.service
systemctl restart thinksmart-ui.service
systemctl is-active --quiet thinksmart-ui.service
if [ -f /etc/thinksmart-voice/aliyun.json ]; then
    timeout 20 sh -c 'until [ -S /run/thinksmart-voice/control.sock ]; do sleep 1; done'
    systemctl is-active --quiet thinksmart-voice.service
fi
systemctl disable --now thinksmart-llm.service >/dev/null 2>&1 || true
systemctl enable --now thinksmart-gateway-maintenance.path thinksmart-gateway.service
curl --fail --silent --max-time 30 http://127.0.0.1:8787/api/settings >/dev/null
rm -rf "$STAGE/backup" "$STAGE/previous-venv" "$STAGE/node-download"
trap - EXIT HUP INT TERM
