# Windows PowerShell 5.1+; copy ZIP and media stay untouched.
# Usage: powershell -ExecutionPolicy Bypass -File .\upload_job.ps1
param(
    [string]$ZipPath = (Join-Path ([Environment]::GetFolderPath("Desktop")) "Concat-Test-GPU.zip"),
    [string]$Domain = ""
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $ZipPath -PathType Leaf)) {
    throw "Concat project ZIP not found: $ZipPath"
}
$ZipPath = (Resolve-Path -LiteralPath $ZipPath).Path
if (-not $Domain) { $Domain = Read-Host "Paste Salad Access Domain HTTPS URL" }
$Domain = $Domain.Trim().TrimEnd('/')
if (-not $Domain.StartsWith("https://", [StringComparison]::OrdinalIgnoreCase)) {
    throw "Salad domain must begin with https://"
}
$uri = [Uri]$Domain
if ($uri.UserInfo -or $uri.Query -or $uri.Fragment -or $uri.AbsolutePath -ne "/") {
    throw "Use the Salad domain origin only (no path or credentials)"
}

$saladSecure = Read-Host "Salad API Key (hidden)" -AsSecureString
$renderSecure = Read-Host "RENDERER_API_TOKEN (hidden)" -AsSecureString
$sPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($saladSecure)
$rPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($renderSecure)
try {
    $saladKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($sPtr)
    $rendererKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($rPtr)
    if ([string]::IsNullOrWhiteSpace($saladKey) -or [string]::IsNullOrWhiteSpace($rendererKey)) {
        throw "Both API credentials are required"
    }
    $headers = @{ "Salad-Api-Key" = $saladKey; "Authorization" = "Bearer $rendererKey" }
    $file = Get-Item -LiteralPath $ZipPath
    $size = [int64]$file.Length
    $sha = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $stateFile = "$ZipPath.upload-state.json"
    $saved = $null
    if (Test-Path -LiteralPath $stateFile) {
        try { $saved = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json } catch { $saved = $null }
    }
    if ($saved -and $saved.sha256 -eq $sha -and $saved.size -eq $size -and $saved.domain -eq $Domain) {
        $job = [string]$saved.job
        Write-Host "Resuming previous Concat job: $job"
    } else {
        $job = "concat-" + [Guid]::NewGuid().ToString("N").Substring(0, 16)
        @{ job = $job; sha256 = $sha; size = $size; domain = $Domain } | ConvertTo-Json -Compress | Set-Content -LiteralPath $stateFile -Encoding UTF8
        Write-Host "Created upload job: $job"
    }
    $base = "$Domain/api/uploads/$job"
    $renderJob = "$Domain/api/jobs/$job"

    # If a previous finalize response was lost, NEVER upload same job twice.
    try {
        $existing = Invoke-RestMethod -Uri $renderJob -Headers $headers -TimeoutSec 30
        Write-Host "Job already submitted: $job / $($existing.state)"
        if ($existing.error) { Write-Host "Job detail: $($existing.error)" }
        return
    } catch {
        $response = $_.Exception.Response
        if (-not $response -or [int]$response.StatusCode -ne 404) { throw }
    }

    $manifest = @{ size = $size; sha256 = $sha } | ConvertTo-Json -Compress
    $session = Invoke-RestMethod -Method Post -Uri $base -Headers $headers -ContentType "application/json" -Body $manifest -TimeoutSec 30
    $chunkSize = [int64]$session.chunk_size
    $totalChunks = [int]$session.total_chunks
    if ($chunkSize -le 0 -or $chunkSize -gt 8388608 -or $totalChunks -lt 1) {
        throw "Worker returned invalid chunk layout"
    }
    $uploaded = @($session.uploaded)
    Write-Host "Uploading $size bytes in $totalChunks chunks; already present: $($uploaded.Count)"
    $stream = [System.IO.File]::OpenRead($ZipPath)
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        for ($i = 0; $i -lt $totalChunks; $i++) {
            if ($uploaded -contains $i) {
                Write-Host "Chunk $($i+1)/$totalChunks already verified (skip)"
                continue
            }
            $offset = [int64]$i * $chunkSize
            $length = [int][Math]::Min($chunkSize, $size - $offset)
            $bytes = New-Object byte[] $length
            [void]$stream.Seek($offset, [System.IO.SeekOrigin]::Begin)
            $read = 0
            while ($read -lt $length) {
                $n = $stream.Read($bytes, $read, $length - $read)
                if ($n -le 0) { throw "Unexpected EOF in project ZIP" }
                $read += $n
            }
            $hashBytes = $hasher.ComputeHash($bytes)
            $chunkSha = ([BitConverter]::ToString($hashBytes)).Replace("-", "").ToLowerInvariant()
            $chunkHeaders = @{ "Salad-Api-Key" = $saladKey; "Authorization" = "Bearer $rendererKey"; "X-Content-SHA256" = $chunkSha }
            $chunkUri = "$base/chunks/$i"
            $ok = $false
            for ($attempt = 1; $attempt -le 3; $attempt++) {
                try {
                    Invoke-RestMethod -Method Put -Uri $chunkUri -Headers $chunkHeaders -ContentType "application/octet-stream" -Body $bytes -TimeoutSec 95 | Out-Null
                    $ok = $true
                    break
                } catch {
                    # A timeout may occur AFTER the server verified and saved a chunk.
                    try {
                        $progress = Invoke-RestMethod -Uri $base -Headers $headers -TimeoutSec 30
                        if (@($progress.uploaded) -contains $i) { $ok = $true; break }
                    } catch {}
                    if ($attempt -eq 3) { throw }
                    Start-Sleep -Seconds (2 * $attempt)
                }
            }
            if (-not $ok) { throw "Could not upload chunk $i" }
            Write-Host "Verified chunk $($i+1)/$totalChunks"
        }
    } finally {
        $stream.Dispose()
        $hasher.Dispose()
    }

    $done = Invoke-RestMethod -Method Post -Uri "$base/complete" -Headers $headers -TimeoutSec 95
    Write-Host "Full ZIP SHA256 verified; render state: $($done.state) / Job: $job"
    $current = Invoke-RestMethod -Uri $renderJob -Headers $headers -TimeoutSec 30
    $current | Select-Object job, state, error, output_size, output_sha256 | Format-List
    Write-Host "To check again, rerun this same script; it will use saved job ID."
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($sPtr)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($rPtr)
    Remove-Variable saladKey, rendererKey, headers -ErrorAction SilentlyContinue
}
