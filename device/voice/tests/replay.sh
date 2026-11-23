#!/bin/sh
# shellcheck disable=SC2029 # remote paths are quoted with quote_sh and expand locally on purpose
# 在设备上用当前工作区代码和真实对话模型回放 replay.py 的用例；参数为用例名关键字，留空跑全部。
set -eu
voice=$(cd "$(dirname "$0")/.." && pwd)
host=${THINKSMART_HOST:-thinksmart}

quote_sh() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

dir=$(ssh "$host" 'mktemp -d /tmp/thinksmart-replay.XXXXXX')
trap 'ssh "$host" "rm -rf -- $(quote_sh "$dir")" >/dev/null 2>&1 || true' EXIT HUP INT TERM

ssh "$host" "mkdir -p -- $(quote_sh "$dir/tests")"
scp -q "$voice"/*.py "$host:$dir/"
scp -q "$voice/tests/replay.py" "$host:$dir/tests/"

args=""
for arg in "$@"; do
    args="$args $(quote_sh "$arg")"
done

ssh "$host" "sudo systemd-run --quiet --wait --pipe --collect \
  -p User=thinksmart -p Group=thinksmart -p SupplementaryGroups=audio \
  -p Environment=XDG_RUNTIME_DIR=/run/user/1000 -p Environment=PULSE_SERVER=unix:/run/user/1000/pulse/native \
  -p Environment=HOME=/tmp -p Environment=PYTHONUNBUFFERED=1 \
  -p EnvironmentFile=-/etc/thinksmart-voice.conf -p EnvironmentFile=-/etc/thinksmart-voice/gateway.conf \
  -p LoadCredential=aliyun.json:/etc/thinksmart-voice/aliyun.json \
  -p LoadCredential=chat.json:/etc/thinksmart-voice/chat.json \
  -p LoadCredential=expert.json:/etc/thinksmart-voice/expert.json \
  /data/thinksmart/voice/venv/bin/python3 $(quote_sh "$dir/tests/replay.py")$args"
