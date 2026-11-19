# Clear the saved relay URL and current-user DPAPI token.
$ErrorActionPreference = "Stop"
$folder = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "MyRemote"
foreach ($name in @("server-url.txt", "agent-token.dpapi")) {
    $path = Join-Path $folder $name
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force
    }
}
Write-Host "Saved My Remote pairing cleared. Close any currently running agent."
Write-Host "The relay token remains valid until it is rotated on the server."
