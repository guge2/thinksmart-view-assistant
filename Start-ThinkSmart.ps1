$ErrorActionPreference = 'Stop'

$sshExe = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'
$sshKey = Join-Path $PSScriptRoot '.local\ssh\thinksmart-admin'
$knownHosts = Join-Path $PSScriptRoot '.local\ssh\known_hosts'

if (-not (Test-Path -LiteralPath $sshExe -PathType Leaf)) {
    throw 'Windows OpenSSH Client is not installed.'
}

if (-not (Test-Path -LiteralPath $sshKey -PathType Leaf)) {
    throw 'ThinkSmart SSH key is missing from .local\ssh.'
}

if (-not (Test-Path -LiteralPath $knownHosts -PathType Leaf)) {
    throw 'ThinkSmart SSH known_hosts file is missing from .local\ssh.'
}

& $sshExe `
    -o 'StrictHostKeyChecking=yes' `
    -o "UserKnownHostsFile=$knownHosts" `
    -o 'IdentitiesOnly=yes' `
    -i $sshKey `
    'thinksmart@192.168.0.109'

exit $LASTEXITCODE
