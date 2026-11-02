param(
    [string]$ClashDataPath = "$env:APPDATA\io.github.clash-verge-rev.clash-verge-rev"
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$sshExe = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'
$scpExe = Join-Path $env:SystemRoot 'System32\OpenSSH\scp.exe'
$sshOptions = @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=yes',
    '-o', 'UserKnownHostsFile=.local/ssh/known_hosts', '-i', '.local/ssh/thinksmart-admin')
$device = 'thinksmart@192.168.0.107'
$stage = '/data/thinksmart/proxy-upload'

function Check-Exit {
    if ($LASTEXITCODE -ne 0) { throw "Command failed with exit code $LASTEXITCODE" }
}

New-Item -ItemType Directory -Force -Path tools/mihomo, work/proxy | Out-Null
python device/proxy/prepare.py --source $ClashDataPath --output work/proxy/export
Check-Exit
$env:THINKSMART_CLASH_DATA = $ClashDataPath
try {
@'
import gzip, hashlib, os, pathlib, shutil, urllib.request, yaml
root = pathlib.Path('tools/mihomo')
archive = root / 'mihomo-linux-arm64-v1.19.33.gz'
if not archive.exists():
    config = yaml.safe_load((pathlib.Path(os.environ['THINKSMART_CLASH_DATA'])/'clash-verge.yaml').read_text(encoding='utf-8'))
    proxy = 'http://127.0.0.1:' + str(config.get('mixed-port',7890))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http':proxy,'https':proxy}))
    with opener.open('https://github.com/MetaCubeX/mihomo/releases/download/v1.19.33/'+archive.name,timeout=60) as src, archive.open('wb') as dst:
        shutil.copyfileobj(src,dst)
if hashlib.sha256(archive.read_bytes()).hexdigest() != 'ba408b317080ee265c46cb923a50018c1b82cbd708551cdd46a1e3e307396187':
    raise RuntimeError('Mihomo archive checksum mismatch')
with gzip.open(archive,'rb') as src, (root/'mihomo').open('wb') as dst:
    shutil.copyfileobj(src,dst)
shutil.copy2(root/'mihomo','work/proxy/export/mihomo')
'@ | python -
    Check-Exit
} finally {
    Remove-Item Env:THINKSMART_CLASH_DATA
}
tar -czf work/proxy/thinksmart-proxy.tar.gz -C work/proxy/export .
Check-Exit
& $sshExe @sshOptions $device "sudo install -d -o thinksmart -g thinksmart -m 0700 $stage"
Check-Exit
& $scpExe @sshOptions work/proxy/thinksmart-proxy.tar.gz device/proxy/install.sh device/proxy/mihomo.service device/proxy/select.py "${device}:$stage/"
Check-Exit
& $sshExe @sshOptions $device "set -e; cd $stage; mkdir -p payload; tar -xzf thinksmart-proxy.tar.gz -C payload; sudo sh ./install.sh $stage/payload; sudo /data/thinksmart/proxy/bin/mihomo -t -d /data/thinksmart/proxy -f /data/thinksmart/proxy/tun.yaml; sudo install -m 0600 /data/thinksmart/proxy/tun.yaml /data/thinksmart/proxy/config.yaml; sudo systemctl restart mihomo; sleep 1; sudo thinksmart-proxy-select; sudo systemctl enable mihomo; systemctl is-active mihomo"
Check-Exit
