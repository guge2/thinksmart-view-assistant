[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateNotNullOrEmpty()]
    [string]$WakeWord
)

$ErrorActionPreference = 'Stop'

if ($WakeWord.Contains("`r") -or $WakeWord.Contains("`n")) {
    throw '唤醒词不能包含换行。'
}

$sshExe = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'
$sshKey = Join-Path $PSScriptRoot '.local\ssh\thinksmart-admin'
$knownHosts = Join-Path $PSScriptRoot '.local\ssh\known_hosts'
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($WakeWord))
$remoteCommand = "printf '%s' '$encoded' | base64 -d | sudo -n /usr/local/sbin/thinksmart-set-wake-word --stdin"

& $sshExe `
    -o 'StrictHostKeyChecking=yes' `
    -o "UserKnownHostsFile=$knownHosts" `
    -o 'IdentitiesOnly=yes' `
    -i $sshKey `
    'thinksmart@192.168.0.107' `
    $remoteCommand

exit $LASTEXITCODE
