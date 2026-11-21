[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateNotNullOrEmpty()]
    [string]$WakeWord
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($WakeWord.Contains("`r") -or $WakeWord.Contains("`n") -or $WakeWord.Contains('@')) {
    throw '唤醒词包含非法字符。'
}

$sshExe = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'
$sshKey = Join-Path $PSScriptRoot '.local\ssh\thinksmart-admin'
$knownHosts = Join-Path $PSScriptRoot '.local\ssh\known_hosts'
foreach ($path in @($sshExe, $sshKey, $knownHosts)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required file is missing: $path"
    }
}
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($WakeWord))

$encoded | & $sshExe `
    -o 'BatchMode=yes' `
    -o 'ConnectTimeout=10' `
    -o 'StrictHostKeyChecking=yes' `
    -o "UserKnownHostsFile=$knownHosts" `
    -o 'IdentitiesOnly=yes' `
    -i $sshKey `
    'thinksmart@192.168.0.109' `
    'base64 -d -i | sudo -n /usr/local/sbin/thinksmart-set-wake-word --stdin'

exit $LASTEXITCODE
