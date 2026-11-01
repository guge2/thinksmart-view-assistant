#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
	exec sudo -n "$0" "$@"
fi

if [ "${1-}" = "--stdin" ]; then
	phrase=$(cat)
elif [ "${1-}" = "--request" ]; then
	# Display requests come from an unprivileged runtime directory: accept only a
	# regular file, consume it before validation, and allow 2-8 CJK characters.
	request=${2-}
	if [ -z "$request" ] || [ ! -f "$request" ] || [ -L "$request" ]; then
		rm -f -- "$request" 2>/dev/null || true
		echo "无效的唤醒词请求" >&2
		exit 2
	fi
	phrase=$(timeout 5 head -c 64 -- "$request" || true)
	rm -f -- "$request"
	if ! printf '%s' "$phrase" | PYTHONIOENCODING=utf-8 /usr/bin/python3 -c 'import re, sys; sys.exit(0 if re.fullmatch("[\u4e00-\u9fff]{2,8}", sys.stdin.read()) else 1)'; then
		echo "唤醒词必须是 2 到 8 个汉字" >&2
		exit 2
	fi
else
	phrase=${1-}
fi

case "$phrase" in
	"")
		echo "唤醒词不能为空" >&2
		exit 2
		;;
	*"
"*)
		echo "唤醒词不能包含换行" >&2
		exit 2
		;;
esac

VOICE_ROOT=/data/thinksmart/voice
MODEL_DIR="$VOICE_ROOT/models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
RAW_FILE="$VOICE_ROOT/keywords.raw.new"
KEYWORDS_FILE="$VOICE_ROOT/keywords.txt.new"
PHRASE_FILE=/etc/thinksmart-voice/wake-phrase

install -d -m 0755 "$VOICE_ROOT" /etc/thinksmart-voice
printf '%s @%s\n' "$phrase" "$phrase" > "$RAW_FILE"
"$VOICE_ROOT/venv/bin/sherpa-onnx-cli" text2token \
	--tokens "$MODEL_DIR/tokens.txt" \
	--tokens-type ppinyin \
	"$RAW_FILE" \
	"$KEYWORDS_FILE"
mv "$KEYWORDS_FILE" "$VOICE_ROOT/keywords.txt"
printf '%s\n' "$phrase" > "$PHRASE_FILE.new"
mv "$PHRASE_FILE.new" "$PHRASE_FILE"
rm -f "$RAW_FILE"
chown thinksmart:thinksmart "$VOICE_ROOT/keywords.txt"
chmod 0644 "$VOICE_ROOT/keywords.txt" "$PHRASE_FILE"
systemctl try-restart thinksmart-voice.service
printf '唤醒词已改为：%s\n' "$phrase"
