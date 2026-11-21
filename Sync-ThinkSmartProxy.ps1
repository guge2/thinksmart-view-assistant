param(
    [string]$ClashDataPath = "$env:APPDATA\io.github.clash-verge-rev.clash-verge-rev"
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Set-Location -LiteralPath $PSScriptRoot
$sshExe = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'
$scpExe = Join-Path $env:SystemRoot 'System32\OpenSSH\scp.exe'
$sshKey = Join-Path $PSScriptRoot '.local\ssh\thinksmart-admin'
$knownHosts = Join-Path $PSScriptRoot '.local\ssh\known_hosts'
foreach ($required in @($sshExe, $scpExe, $sshKey, $knownHosts)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "Required file not found: $required" }
}
$sshOptions = @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'IdentitiesOnly=yes',
    '-o', 'StrictHostKeyChecking=yes', '-o', "UserKnownHostsFile=$knownHosts", '-i', $sshKey)
$device = 'thinksmart@192.168.0.109'
$stage = '/data/thinksmart/proxy-upload'

function Assert-ExitCode {
    if ($LASTEXITCODE -ne 0) { throw "Command failed with exit code $LASTEXITCODE" }
}

New-Item -ItemType Directory -Force -Path tools/mihomo, work/proxy | Out-Null
$exportDir = 'work/proxy/export'
$bundle = 'work/proxy/thinksmart-proxy.tar.gz'
if (Test-Path -LiteralPath $exportDir) { Remove-Item -Recurse -Force -LiteralPath $exportDir }
if (Test-Path -LiteralPath $bundle) { Remove-Item -Force -LiteralPath $bundle }

$remoteCreated = $false
try {
    python device/proxy/prepare.py --source $ClashDataPath --output $exportDir
    Assert-ExitCode
    $prevClashData = $env:THINKSMART_CLASH_DATA
    $env:THINKSMART_CLASH_DATA = $ClashDataPath
    try {
@'
import gzip, hashlib, os, pathlib, shutil, urllib.request, yaml
root = pathlib.Path('tools/mihomo')
archive = root / 'mihomo-linux-arm64-v1.19.33.gz'
expected = 'ba408b317080ee265c46cb923a50018c1b82cbd708551cdd46a1e3e307396187'
if not archive.exists():
    config = yaml.safe_load((pathlib.Path(os.environ['THINKSMART_CLASH_DATA'])/'clash-verge.yaml').read_text(encoding='utf-8')) or {}
    port = int(config.get('mixed-port', 7890))
    proxy = f'http://127.0.0.1:{port}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http':proxy,'https':proxy}))
    part = archive.with_suffix('.gz.part')
    try:
        with opener.open('https://github.com/MetaCubeX/mihomo/releases/download/v1.19.33/'+archive.name, timeout=60) as src, part.open('wb') as dst:
            shutil.copyfileobj(src, dst)
        if hashlib.sha256(part.read_bytes()).hexdigest() != expected:
            raise RuntimeError('Mihomo archive checksum mismatch')
        part.replace(archive)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
    raise RuntimeError('Mihomo archive checksum mismatch')
with gzip.open(archive, 'rb') as src, (root / 'mihomo').open('wb') as dst:
    shutil.copyfileobj(src, dst)
shutil.copy2(root / 'mihomo', 'work/proxy/export/mihomo')
'@ | python -
        Assert-ExitCode
    } finally {
        if ($null -eq $prevClashData) {
            Remove-Item Env:THINKSMART_CLASH_DATA -ErrorAction SilentlyContinue
        } else {
            $env:THINKSMART_CLASH_DATA = $prevClashData
        }
    }
    tar -czf $bundle -C $exportDir .
    Assert-ExitCode
    & $sshExe @sshOptions $device "sudo -n rm -rf $stage && sudo -n install -d -o thinksmart -g thinksmart -m 0700 $stage"
    Assert-ExitCode
    $remoteCreated = $true
    & $scpExe @sshOptions $bundle device/proxy/install.sh device/proxy/mihomo.service device/proxy/select.py "${device}:$stage/"
    Assert-ExitCode
    & $sshExe @sshOptions $device "set -e; cd $stage; rm -rf payload; mkdir -p payload; tar -xzf thinksmart-proxy.tar.gz -C payload; sudo -n sh ./install.sh $stage/payload; sudo -n /data/thinksmart/proxy/bin/mihomo -t -d /data/thinksmart/proxy -f /data/thinksmart/proxy/tun.yaml; sudo -n install -m 0600 /data/thinksmart/proxy/tun.yaml /data/thinksmart/proxy/config.yaml; sudo -n systemctl restart mihomo; sudo -n thinksmart-proxy-select; sudo -n systemctl enable mihomo; systemctl is-active mihomo"
    Assert-ExitCode
} finally {
    if ($remoteCreated) {
        & $sshExe @sshOptions $device "sudo -n rm -rf $stage" 2>$null | Out-Null
    }
    if (Test-Path -LiteralPath $exportDir) { Remove-Item -Recurse -Force -LiteralPath $exportDir -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $bundle) { Remove-Item -Force -LiteralPath $bundle -ErrorAction SilentlyContinue }
}
