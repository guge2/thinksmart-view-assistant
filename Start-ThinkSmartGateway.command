#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
umask 077
mkdir -p work/gateway
key="$PWD/.local/ssh/thinksmart-admin"
hosts="$PWD/.local/ssh/known_hosts"
device=thinksmart@192.168.0.109
port=${THINKSMART_GATEWAY_PORT:-18781}
case "$port" in *[!0-9]*|'') echo '端口必须是数字。' >&2; exit 1;; esac
control="$PWD/work/gateway/ssh-control-$port"
if [ "${1-}" = '--close' ]; then
    ssh -S "$control" -O exit "$device" 2>/dev/null || true
    exit 0
fi
if [ ! -f "$key" ] || [ ! -f "$hosts" ]; then
    echo '缺少 .local/ssh 中的设备登录密钥或 known_hosts。' >&2
    exit 1
fi
if ! ssh -S "$control" -O check "$device" >/dev/null 2>&1; then
    ssh -M -S "$control" -fNT \
        -o BatchMode=yes -o ConnectTimeout=8 -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \
        -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$hosts" -o IdentitiesOnly=yes \
        -o HostKeyAlias=192.168.0.107 \
        -i "$key" -L "127.0.0.1:$port:127.0.0.1:8787" "$device"
fi
url="http://127.0.0.1:$port/"
if ! curl --silent --fail --max-time 8 "$url" >/dev/null; then
    echo '管理网关暂时未响应，请确认设备已安装并启动网关服务。' >&2
    exit 1
fi
printf 'ThinkSmart 管理页面：%s\n' "$url"
if [ "${1-}" != '--no-open' ]; then
    open "$url"
fi
