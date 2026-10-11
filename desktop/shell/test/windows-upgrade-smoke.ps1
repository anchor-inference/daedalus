# A real Windows handoff from the application's Install action. The package and data live under
# RUNNER_TEMP; the launcher is given an older version so it can install the signed public release.
param(
    [Parameter(Mandatory = $true)][string]$Archive,
    [string]$ExpectedTag = ''
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Fail([string]$message) { throw "windows-upgrade-smoke: $message" }

$root = Join-Path $env:RUNNER_TEMP 'daedalus-upgrade-smoke'
$app = Join-Path $root 'app'
$data = Join-Path $root 'profile\Daedalus\data'
$env:LOCALAPPDATA = Join-Path $root 'profile'
$env:DAEDALUS_LOCAL_ROOT = Join-Path $root 'local'
$env:DAEDALUS_UPGRADE_FIXTURE = 'ok'
$env:DAEDALUS_UPGRADE_FIXTURE_HANDOVER_PAUSE = '15'
Remove-Item -Recurse -Force $root -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $app, $data | Out-Null
Expand-Archive -Path (Resolve-Path $Archive).Path -DestinationPath $app
Set-Content -NoNewline -Encoding ascii (Join-Path $data 'mode') 'native'
New-Item -ItemType File (Join-Path $data '.upgrade-fixture') | Out-Null

# This is the packaged application under test, with a fixture launcher that advertises the
# preceding version. The fixture skips runtime preparation while the real archive and signature,
# file swap, finish handoff and application restart all run as they do for an operator.
$launcher = Join-Path $app 'daedalus-desktop.exe'
Push-Location desktop
try {
    & go build -tags nowebview,upgradefixture -ldflags '-X main.version=desktop-v0.16.1' -o $launcher .
    if ($LASTEXITCODE -ne 0) { Fail "fixture launcher build exited $LASTEXITCODE" }
} finally { Pop-Location }
if ((& $launcher --version) -ne 'desktop-v0.16.1') { Fail 'fixture launcher has the wrong version' }

$process = Start-Process -FilePath (Join-Path $app 'Daedalus.exe') -ArgumentList '--remote-debugging-port=19434' -PassThru
$base = $null
for ($i = 0; $i -lt 90 -and -not $base; $i++) {
    Start-Sleep 1
    try {
        $base = @(Invoke-RestMethod 'http://127.0.0.1:19434/json/list' -TimeoutSec 2 |
            Where-Object { $_.type -eq 'page' -and $_.url -match '^http://127\.0\.0\.1:\d+/' } |
            ForEach-Object { [uri]$_.url }) | Select-Object -First 1
    } catch {}
}
if (-not $base) { Fail 'the application did not show a launcher page' }
$base = "$($base.Scheme)://$($base.Authority)"
$page = (Invoke-WebRequest -UseBasicParsing "$base/setup").Content
if ($page -notmatch '<meta name="csrf" content="([^"]+)"') { Fail 'the launcher page has no action token' }
$headers = @{ 'X-Daedalus-Desktop' = $Matches[1] }

Invoke-WebRequest -UseBasicParsing -Method Post -Headers $headers "$base/api/action/check-upgrade" | Out-Null
$status = Invoke-RestMethod "$base/api/status"
if (-not $ExpectedTag) { $ExpectedTag = $status.upgrade.to }
if ($ExpectedTag -notmatch '^desktop-v\d+\.\d+\.\d+$') { Fail "there is no published upgrade: $ExpectedTag" }
if ($status.upgrade.to -ne $ExpectedTag) { Fail "the launcher offered $($status.upgrade.to), expected $ExpectedTag" }
Invoke-WebRequest -UseBasicParsing -Method Post -Headers $headers "$base/api/action/download" | Out-Null
for ($i = 0; $i -lt 900; $i++) {
    Start-Sleep 1
    $status = Invoke-RestMethod "$base/api/status"
    if ($status.download.state -eq 'failed') { Fail "download failed: $($status.download.error)" }
    if ($status.download.ready) { break }
}
if (-not $status.download.ready) { Fail 'the signed release was not downloaded in 15 minutes' }
Write-Host "$ExpectedTag downloaded and verified"

Invoke-WebRequest -UseBasicParsing -Method Post -Headers $headers "$base/api/action/upgrade" | Out-Null
for ($i = 0; $i -lt 90 -and -not $process.HasExited; $i++) { Start-Sleep 1 }
if (-not $process.HasExited) { Fail 'Electron stayed open after Install' }

$upgradeLog = @(Get-ChildItem -Recurse (Join-Path $env:DAEDALUS_LOCAL_ROOT 'state') -Filter upgrade.log |
    Select-Object -First 1 -ExpandProperty FullName)
if (-not $upgradeLog) { Fail 'the detached upgrade process did not start' }
$sawProgress = $false
$sawProgressPage = $false
for ($i = 0; $i -lt 900; $i++) {
    Start-Sleep 1
    if (-not $sawProgressPage) {
        $upgrader = Get-CimInstance Win32_Process | Where-Object {
            $_.Name -eq 'daedalus-desktop.exe' -and $_.CommandLine -match '--from-app'
        } | Select-Object -First 1
        if ($upgrader) {
            foreach ($port in @(Get-NetTCPConnection -OwningProcess $upgrader.ProcessId -State Listen -ErrorAction SilentlyContinue |
                Where-Object { $_.LocalAddress -eq '127.0.0.1' } | Select-Object -ExpandProperty LocalPort)) {
                try {
                    $progress = Invoke-RestMethod "http://127.0.0.1:$port/progress" -TimeoutSec 2
                    if ($progress.state -eq 'running' -and $progress.steps.Count -gt 0) { $sawProgressPage = $true }
                } catch {}
            }
        }
    }
    $text = Get-Content -Raw $upgradeLog
    if ($text -match 'Handing over to ') { $sawProgress = $true }
    if ($text -match "Upgraded to $([regex]::Escape($ExpectedTag))\.") { break }
    if ($text -match 'Rolling back:|nothing was changed:') { Fail "upgrade failed: $text" }
}
if (-not $sawProgress) { Fail "the upgrade did not reach the file swap: $text" }
if (-not $sawProgressPage) { Fail 'the upgrade progress page never answered on localhost' }
if ($text -notmatch "Upgraded to $([regex]::Escape($ExpectedTag))\.") { Fail "the upgrade did not commit: $text" }
if ((& $launcher --version) -ne $ExpectedTag) { Fail 'the old launcher is still installed' }
Write-Host "Installed $ExpectedTag through the application after Electron exited"
