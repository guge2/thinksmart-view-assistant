#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
	exec sudo -n "$0" "$@"
fi

if [ "${1-}" = "--stdin" ]; then
	phrase=$(cat)
elif [ "${1-}" = "--request" ]; then
	# Display requests come from an unprivileged runtime directory: open without
	# following symlinks, verify it is a regular file, consume it, and validate.
	request=${2-}
	if [ -z "$request" ]; then
		echo "无效的唤醒词请求" >&2
		exit 2
	fi
	phrase=$(PYTHONIOENCODING=utf-8 /usr/bin/python3 - "$request" <<'PY'
import os, re, stat, sys
path = sys.argv[1]
try:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not regular")
        raw = os.read(fd, 65)
    finally:
        os.close(fd)
finally:
    try:
        os.unlink(path)
    except OSError:
        pass
text = raw.decode("utf-8", "strict")
if not re.fullmatch(r"[\u4e00-\u9fff]{2,8}", text):
    sys.stderr.write("唤醒词必须是 2 到 8 个汉字\n")
    sys.exit(2)
sys.stdout.write(text)
PY
	)
else
	phrase=${1-}
fi

case "$phrase" in
	"")
		echo "唤醒词不能为空" >&2
		exit 2
		;;
	*"
"*|*"@"*)
		echo "唤醒词包含非法字符" >&2
		exit 2
		;;
esac

VOICE_ROOT=/data/thinksmart/voice
MODEL_DIR="$VOICE_ROOT/models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
PHRASE_FILE=/etc/thinksmart-voice/wake-phrase

WORK=$(mktemp -d)
chmod 0700 "$WORK"
trap 'rm -rf -- "$WORK"' EXIT HUP INT TERM

RAW_FILE="$WORK/keywords.raw"
KEYWORDS_FILE="$WORK/keywords.txt"

install -d -o root -g root -m 0755 "$VOICE_ROOT" /etc/thinksmart-voice
printf '%s @%s\n' "$phrase" "$phrase" > "$RAW_FILE"
"$VOICE_ROOT/venv/bin/sherpa-onnx-cli" text2token \
	--tokens "$MODEL_DIR/tokens.txt" \
	--tokens-type ppinyin \
	"$RAW_FILE" \
	"$KEYWORDS_FILE"
if [ "$phrase" = "小爱同学" ]; then
	cat << 'EOF' >> "$KEYWORDS_FILE"
x iào ài t óng x ué @小爱同学
x iǎo y ī t óng x ué @小爱同学
x iào y ī t óng x ué @小爱同学
EOF
fi

# Atomic replace inside root-owned directories so unprivileged users cannot race symlinks.
install -o root -g root -m 0644 "$KEYWORDS_FILE" "$VOICE_ROOT/.keywords.txt.new.$$"
mv -f "$VOICE_ROOT/.keywords.txt.new.$$" "$VOICE_ROOT/keywords.txt"
printf '%s\n' "$phrase" > "$WORK/wake-phrase"
install -o root -g root -m 0644 "$WORK/wake-phrase" "$PHRASE_FILE.new.$$"
mv -f "$PHRASE_FILE.new.$$" "$PHRASE_FILE"
systemctl try-restart thinksmart-voice.service
printf '唤醒词已改为：%s\n' "$phrase"
