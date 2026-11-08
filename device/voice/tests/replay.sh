#!/bin/sh
# 在设备上用当前工作区代码和真实对话模型回放 replay.py 的用例；参数为用例名关键字，留空跑全部。
set -eu
voice=$(cd "$(dirname "$0")/.." && pwd)
host=${THINKSMART_HOST:-thinksmart}
dir=/tmp/thinksmart-replay

ssh "$host" "rm -rf $dir && mkdir -p $dir/tests"
scp -q "$voice"/*.py "$host:$dir/"
scp -q "$voice/tests/replay.py" "$host:$dir/tests/"
ssh "$host" sudo systemd-run --quiet --wait --pipe --collect \
  -p User=thinksmart -p Group=thinksmart -p SupplementaryGroups=audio \
  -p Environment=XDG_RUNTIME_DIR=/run/user/1000 -p Environment=PULSE_SERVER=unix:/run/user/1000/pulse/native \
  -p Environment=HOME=/tmp -p Environment=PYTHONUNBUFFERED=1 \
  -p EnvironmentFile=-/etc/thinksmart-voice.conf -p EnvironmentFile=-/etc/thinksmart-voice/gateway.conf \
  -p LoadCredential=aliyun.json:/etc/thinksmart-voice/aliyun.json \
  -p LoadCredential=chat.json:/etc/thinksmart-voice/chat.json \
  -p LoadCredential=expert.json:/etc/thinksmart-voice/expert.json \
  /data/thinksmart/voice/venv/bin/python3 $dir/tests/replay.py "$@"
