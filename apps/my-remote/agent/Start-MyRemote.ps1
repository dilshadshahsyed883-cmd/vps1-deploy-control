# My Remote agent launcher for Windows PowerShell 5.1+.
# First run pairs the agent with a URL and a private agent token.
# ConvertFrom-SecureString uses Windows DPAPI for the current Windows user.
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$settingsDirectory = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "MyRemote"
$urlPath = Join-Path $settingsDirectory "server-url.txt"
$tokenPath = Join-Path $settingsDirectory "agent-token.dpapi"
$exePath = Join-Path $PSScriptRoot "MyRemote.Agent.exe"

function Validate-ServerUrl([string] $raw) {
    $parsed = $null
    if (-not [Uri]::TryCreate($raw, [UriKind]::Absolute, [ref] $parsed)) {
        throw "Please enter a complete HTTPS URL, e.g. https://remote.example.com."
    }
    if ($parsed.Scheme -ne "https" -or $parsed.UserInfo -ne "" -or $parsed.AbsolutePath -ne "/" -or
        $parsed.Query -ne "" -or $parsed.Fragment -ne "") {
        throw "Use only a secure HTTPS origin without username, path, query, or fragment."
    }
    if ($parsed.Host -eq "localhost" -or $parsed.Host -eq "127.0.0.1") {
        throw "The server must be reachable over the internet, not localhost."
    }
    return $parsed.GetLeftPart([UriPartial]::Authority).TrimEnd("/")
}

if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "MyRemote.Agent.exe is missing. Keep this launcher in the same folder as the EXE."
}

New-Item -ItemType Directory -Force -Path $settingsDirectory | Out-Null

if ((-not (Test-Path -LiteralPath $urlPath -PathType Leaf)) -or
    (-not (Test-Path -LiteralPath $tokenPath -PathType Leaf))) {
    Write-Host ""
    Write-Host "My Remote - first-time pairing" -ForegroundColor Cyan
    Write-Host "Only pair with your own HTTPS relay. Do not share agent credentials."
    $url = Validate-ServerUrl (Read-Host "Relay HTTPS URL")
    $secret = Read-Host "Private agent token (not your Android password)" -AsSecureString
    if ($secret.Length -lt 32) { throw "Agent token must be at least 32 characters." }

    # Windows encrypts token for the current user on this machine.
    $protected = ConvertFrom-SecureString -SecureString $secret
    Set-Content -LiteralPath $tokenPath -Value $protected -Encoding ASCII -NoNewline
    Set-Content -LiteralPath $urlPath -Value $url -Encoding ASCII -NoNewline
    Write-Host "Pairing saved for the current Windows account." -ForegroundColor Green
}

$url = Validate-ServerUrl (Get-Content -LiteralPath $urlPath -Raw)
try {
    $secret = ConvertTo-SecureString (Get-Content -LiteralPath $tokenPath -Raw) -ErrorAction Stop
} catch {
    throw "Cannot read the encrypted token for this Windows user. Run Reset-MyRemote.ps1 and pair again."
}

$unmanaged = [IntPtr]::Zero
try {
    $unmanaged = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($unmanaged)
    if ([string]::IsNullOrWhiteSpace($token) -or $token.Length -lt 32) {
        throw "Invalid saved agent token."
    }
    $env:REMOTE_SERVER_URL = $url.Replace("https://", "wss://")
    $env:REMOTE_AGENT_TOKEN = $token
    Remove-Variable token -ErrorAction SilentlyContinue

    Write-Host ""
    Write-Host "Connecting to $url" -ForegroundColor Cyan
    Write-Host "Keep this window open while remotely controlling the PC."
    Write-Host "Press Ctrl+C to stop. Your PC must stay signed in and unlocked."
    & $exePath
    if ($LASTEXITCODE -ne 0) {
        throw "MyRemote.Agent.exe exited with code $LASTEXITCODE."
    }
} finally {
    if ($unmanaged -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($unmanaged)
    }
    Remove-Item Env:REMOTE_SERVER_URL -ErrorAction SilentlyContinue
    Remove-Item Env:REMOTE_AGENT_TOKEN -ErrorAction SilentlyContinue
}
