[CmdletBinding()]
param(
    [int]$Port = 9222,
    [int]$TimeoutSeconds = 20,
    [string]$ProfilePath = 'C:\temp\chrome-cdp-profile'
)

$ErrorActionPreference = 'Stop'
$cdpUri = "http://127.0.0.1:$Port/json/version"

function Get-CdpBrowser {
    try {
        return (Invoke-RestMethod -TimeoutSec 2 -Uri $cdpUri).Browser
    }
    catch {
        return $null
    }
}

$browser = Get-CdpBrowser
if ($browser) {
    Write-Output "CDP already available: $browser"
    exit 0
}

$chromeCandidates = @(
    $env:CHROME_BINARY,
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LocalAppData\Google\Chrome\Application\chrome.exe"
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

if (-not $chromeCandidates) {
    throw 'Google Chrome was not found. Install Chrome or provide an authorized CDP browser on port 9222.'
}

$chromePath = $chromeCandidates | Select-Object -First 1
New-Item -ItemType Directory -Force -Path $ProfilePath | Out-Null
$chromeArguments = @(
    "--remote-debugging-port=$Port",
    '--remote-debugging-address=127.0.0.1',
    ('--user-data-dir="' + $ProfilePath + '"'),
    '--no-first-run',
    '--no-default-browser-check',
    'https://dzen.ru'
)

# Do not close or reuse the user's ordinary Chrome windows. The dedicated profile
# retains the Dzen session that was created during the initial interactive login.
Start-Process -FilePath $chromePath -ArgumentList $chromeArguments -WindowStyle Normal

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
do {
    Start-Sleep -Milliseconds 500
    $browser = Get-CdpBrowser
    if ($browser) {
        Write-Output "CDP ready: $browser"
        exit 0
    }
} while ((Get-Date) -lt $deadline)

throw "Chrome started but CDP did not become available on port $Port within $TimeoutSeconds seconds. The dedicated profile may be locked or require an interactive Dzen login."
