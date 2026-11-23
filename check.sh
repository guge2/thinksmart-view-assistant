#!/bin/sh
# 本地和 CI 共用的检查：ruff、shellcheck、单元测试。开发依赖装在 tools/dev（见 AGENTS.md）。
set -eu
cd "$(dirname "$0")"
if [ -x tools/dev/bin/python ]; then
    PATH="$PWD/tools/dev/bin:$PATH"
fi

ruff check device device/audio/thinksmart-volume-keys
git ls-files '*.sh' | xargs shellcheck
for suite in device/voice device/gateway device/ui; do
    (cd "$suite" && python -m unittest discover -s tests -q)
done
