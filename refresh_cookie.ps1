$CloudUrl = "https://irctc-vacancy-mcp-358073315467.us-central1.run.app"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$EnvFile = Join-Path $ScriptDir "irctc_cookie.env"

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  IRCTC Cookie Refresh" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

Write-Host "[1/3] Opening Chrome and capturing cookies..." -ForegroundColor Yellow
Write-Host "      Chrome will open automatically. Do NOT close it." -ForegroundColor Gray
Write-Host ""

python "$ScriptDir\grab_cookie.py"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: grab_cookie.py failed." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host ""
Write-Host "[2/3] Waiting 5 more seconds for page to settle..." -ForegroundColor Yellow
Start-Sleep -Seconds 5
Write-Host "      Done." -ForegroundColor Gray

Write-Host ""
Write-Host "[3/3] Pushing cookie to cloud server..." -ForegroundColor Yellow

if (-not (Test-Path $EnvFile)) {
    Write-Host "ERROR: irctc_cookie.env not found." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

$raw = Get-Content $EnvFile -Raw
$pattern = [regex]::Escape('$env:IRCTC_COOKIE=') + '"(.+)"'
$match = [regex]::Match($raw, $pattern)
if (-not $match.Success) {
    Write-Host "ERROR: Could not parse cookie from irctc_cookie.env" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}
$cookie = $match.Groups[1].Value
Write-Host "      Cookie length: $($cookie.Length) chars" -ForegroundColor Gray

$bodyObj = [PSCustomObject]@{ cookie = $cookie }
$body = $bodyObj | ConvertTo-Json -Compress

try {
    $response = Invoke-RestMethod `
        -Uri "$CloudUrl/set-cookie" `
        -Method POST `
        -ContentType "application/json" `
        -Body $body `
        -TimeoutSec 30

    Write-Host ""
    Write-Host "============================================" -ForegroundColor Green
    Write-Host "  SUCCESS! Cookie pushed to cloud server." -ForegroundColor Green
    Write-Host "  Cookie length: $($response.cookie_length)" -ForegroundColor Green
    Write-Host "============================================" -ForegroundColor Green
} catch {
    Write-Host "ERROR pushing to cloud: $_" -ForegroundColor Red
    Write-Host "Cookie is saved in irctc_cookie.env locally." -ForegroundColor Yellow
}

Write-Host ""
Read-Host "Done. Press Enter to close"
