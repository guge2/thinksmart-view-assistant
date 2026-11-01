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
MODEL_NAME=sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01
MODEL_DIR="$MODEL_ROOT/$MODEL_NAME"
LEGACY_DATA_MODEL="/data/thinksmart/models/kws/$MODEL_NAME"
LEGACY_OPT_MODEL="/opt/thinksmart-voice/models/$MODEL_NAME"
CACHE_ROOT=/var/cache/thinksmart-voice
MODEL_ARCHIVE="$CACHE_ROOT/$MODEL_NAME.tar.bz2"
MODEL_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/$MODEL_NAME.tar.bz2"

NODE_VERSION=22.23.4
NODE_NAME="node-v$NODE_VERSION-linux-arm64"
NODE_URL="${THINKSMART_NODE_MIRROR:-https://npmmirror.com/mirrors/node}/v$NODE_VERSION"
NODE_ROOT=/opt/node
AGENT_SRC="$BASE/../agent/dist/agent.mjs"
AGENT_ROOT=/usr/local/lib/thinksmart-agent

UI_SRC="$BASE/../ui"
UI_ROOT=/usr/local/lib/thinksmart-ui
UI_DATA=/data/thinksmart/ui
UI_MODULES="app.py fbdev.py ime.py system.py touch.py"
CLOCK_SRC="$BASE/../clock"

for module in $UI_MODULES; do
	if [ ! -f "$UI_SRC/$module" ]; then
		echo "缺少界面源文件：$UI_SRC/$module" >&2
		exit 1
	fi
done
if [ ! -f "$AGENT_SRC" ]; then
	echo "缺少智能体程序：$AGENT_SRC（先在 device/agent 运行 npm install && npm run build）" >&2
	exit 1
fi
for file in thinksmart-clock.py thinksmart-clock-restore.service thinksmart-clock-save.service thinksmart-clock-save.timer; do
	if [ ! -f "$CLOCK_SRC/$file" ]; then
		echo "缺少时间服务源文件：$CLOCK_SRC/$file" >&2
		exit 1
	fi
done

systemctl stop thinksmart-ui.service >/dev/null 2>&1 || true
systemctl stop thinksmart-voice.service >/dev/null 2>&1 || true
systemctl disable --now thinksmart-llm.service >/dev/null 2>&1 || true

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
	python3-venv curl ca-certificates bzip2
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
	python3-pil fonts-wqy-microhei fonts-dejavu-core fonts-dejavu-extra libmpg123-0t64 espeak-ng

install -d -m 0755 "$APP_ROOT" "$DATA_ROOT" "$MODEL_ROOT" "$CACHE_ROOT"
install -d -m 0755 /etc/thinksmart-voice
install -m 0755 "$BASE/assistant.py" "$APP_ROOT/assistant.py"
install -m 0644 "$BASE/music.py" "$APP_ROOT/music.py"
install -m 0644 "$BASE/alarms.py" "$APP_ROOT/alarms.py"
install -m 0644 "$BASE/offline.py" "$BASE/information.py" "$BASE/brain.py" "$APP_ROOT/"
install -d -m 0755 "$AGENT_ROOT"
install -m 0644 "$AGENT_SRC" "$AGENT_ROOT/agent.mjs"
if [ "$("$NODE_ROOT/bin/node" --version 2>/dev/null)" != "v$NODE_VERSION" ]; then
	NODE_TMP="$DATA_ROOT/node-download"
	rm -rf "$NODE_TMP"
	install -d -m 0700 "$NODE_TMP"
	curl -fL --retry 3 -o "$NODE_TMP/$NODE_NAME.tar.gz" "$NODE_URL/$NODE_NAME.tar.gz"
	curl -fsSL --retry 3 -o "$NODE_TMP/SHASUMS256.txt" "$NODE_URL/SHASUMS256.txt"
	(cd "$NODE_TMP" && grep " $NODE_NAME.tar.gz$" SHASUMS256.txt | sha256sum -c -)
	tar -xzf "$NODE_TMP/$NODE_NAME.tar.gz" -C "$NODE_TMP" "$NODE_NAME/bin/node" "$NODE_NAME/LICENSE"
	install -d -m 0755 "$NODE_ROOT/bin"
	install -m 0755 "$NODE_TMP/$NODE_NAME/bin/node" "$NODE_ROOT/bin/node"
	install -m 0644 "$NODE_TMP/$NODE_NAME/LICENSE" "$NODE_ROOT/LICENSE"
	rm -rf "$NODE_TMP"
fi
install -d -o thinksmart -g thinksmart -m 0700 /data/thinksmart/music
install -m 0755 "$BASE/configure_voice.py" /usr/local/sbin/thinksmart-configure-voice
install -m 0755 "$BASE/set-wake-word.sh" /usr/local/sbin/thinksmart-set-wake-word
install -m 0644 "$BASE/thinksmart-voice.service" /etc/systemd/system/thinksmart-voice.service
install -m 0644 "$BASE/thinksmart-voice.conf" /etc/thinksmart-voice.conf

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

rm -f /etc/systemd/system/thinksmart-llm.service
rm -f "$APP_ROOT/speech_worker.py"
rm -f /opt/thinksmart-voice/satellite.py /opt/thinksmart-voice/voice_daemon.py

if [ ! -x "$VENV/bin/python3" ]; then
	python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --no-cache-dir \
	"dashscope>=2.0.1,<3" \
	"sherpa-onnx==2.0.1" \
	"sentencepiece>=0.2,<1" \
	"pypinyin>=0.55,<1" \
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

"$VENV/bin/sherpa-onnx-cli" text2token \
	--tokens "$MODEL_DIR/tokens.txt" --tokens-type ppinyin \
	"$BASE/offline-keywords.txt" "$DATA_ROOT/offline-keywords.txt"

if [ ! -s /etc/thinksmart-voice/wake-phrase ]; then
	printf '%s\n' '小爱同学' > /etc/thinksmart-voice/wake-phrase
fi
/usr/local/sbin/thinksmart-set-wake-word --stdin < /etc/thinksmart-voice/wake-phrase

chown -R thinksmart:thinksmart "$DATA_ROOT"
chmod 0644 /etc/thinksmart-voice/wake-phrase "$DATA_ROOT/keywords.txt"
systemctl daemon-reload
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
