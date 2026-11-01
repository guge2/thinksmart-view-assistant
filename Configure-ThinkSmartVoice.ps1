[CmdletBinding()]
param(
    [string]$WorkspaceId
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($WorkspaceId)) {
    $WorkspaceId = Read-Host 'Aliyun Model Studio Workspace ID (cn-beijing)'
}
$WorkspaceId = $WorkspaceId.Trim()
if ($WorkspaceId -notmatch '^[A-Za-z0-9-]{3,128}$') {
    throw 'Invalid Workspace ID.'
}

$secureApiKey = Read-Host 'Aliyun Model Studio API Key (hidden)' -AsSecureString
$sshExe = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'
$sshKey = Join-Path $PSScriptRoot '.local\ssh\thinksmart-admin'
$knownHosts = Join-Path $PSScriptRoot '.local\ssh\known_hosts'

foreach ($path in @($sshExe, $sshKey, $knownHosts)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required file is missing: $path"
    }
}

$bstr = [IntPtr]::Zero
$apiKey = $null
$json = $null
$encoded = $null
$bytes = $null
try {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureApiKey)
    $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    if ($apiKey.Length -lt 16 -or $apiKey.Length -gt 512 -or $apiKey -match '\s') {
        throw 'Invalid API Key.'
    }

    $json = @{
        api_key = $apiKey
        workspace_id = $WorkspaceId
        region = 'cn-beijing'
    } | ConvertTo-Json -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    $encoded = [Convert]::ToBase64String($bytes)

    $encoded | & $sshExe `
        -o 'StrictHostKeyChecking=yes' `
        -o "UserKnownHostsFile=$knownHosts" `
        -o 'IdentitiesOnly=yes' `
        -i $sshKey `
        'thinksmart@192.168.0.107' `
        'base64 -d -i | sudo -n /usr/local/sbin/thinksmart-configure-voice'

    $result = $LASTEXITCODE
    if ($result -ne 0) {
        throw "Device configuration failed with SSH exit code $result."
    }
}
finally {
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    if ($null -ne $bytes) {
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
    $apiKey = $null
    $json = $null
    $encoded = $null
    $secureApiKey = $null
}
