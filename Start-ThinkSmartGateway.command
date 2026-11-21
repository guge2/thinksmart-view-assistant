#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
umask 077
key="$PWD/.local/ssh/thinksmart-admin"
hosts="$PWD/.local/ssh/known_hosts"
device=${THINKSMART_DEVICE:-thinksmart@192.168.0.109}
host_key_alias=${THINKSMART_HOST_KEY_ALIAS:-192.168.0.107}
port=${THINKSMART_GATEWAY_PORT:-18781}
case "$port" in
    *[!0-9]*|'') echo '端口必须是数字。' >&2; exit 1 ;;
esac
if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
    echo '端口必须在 1 到 65535 之间。' >&2
    exit 1
fi
case "${1-}" in
    ''|--close|--no-open) ;;
    *) echo "不支持的参数：$1（可用：--close、--no-open）" >&2; exit 2 ;;
esac
control="${TMPDIR:-/tmp}/thinksmart-gw-$(id -u)-$port.sock"
if [ "${1-}" = '--close' ]; then
    ssh -S "$control" -O exit "$device" 2>/dev/null || true
    rm -f -- "$control"
    exit 0
fi
if [ ! -f "$key" ] || [ ! -f "$hosts" ]; then
    echo '缺少 .local/ssh 中的设备登录密钥或 known_hosts。' >&2
    exit 1
fi
if ! ssh -S "$control" -O check "$device" >/dev/null 2>&1; then
    rm -f -- "$control"
    ssh -M -S "$control" -fNT \
        -o BatchMode=yes -o ConnectTimeout=8 -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \
        -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$hosts" -o IdentitiesOnly=yes \
        -o "HostKeyAlias=$host_key_alias" \
        -i "$key" -L "127.0.0.1:$port:127.0.0.1:8787" "$device"
fi
url="http://127.0.0.1:$port/"
if ! curl --noproxy '*' --silent --fail --max-time 8 "$url" >/dev/null; then
    echo '管理网关暂时未响应，请确认设备已安装并启动网关服务。' >&2
    exit 1
fi
printf 'ThinkSmart 管理页面：%s\n' "$url"
if [ "${1-}" != '--no-open' ]; then
    if command -v open >/dev/null 2>&1; then
        open "$url"
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$url"
    fi
fi
