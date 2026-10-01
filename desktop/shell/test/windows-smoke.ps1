# The Windows installer on a throwaway machine (the release workflow's runner): a silent install,
# what the operator would find — the program, the Start menu and desktop shortcuts, the entry in
# Apps & features — the application started until its window shows the launcher's own page, closed
# again with nothing of it left running, then two uninstalls: the default one keeps the data, the
# one asked to delete it deletes it.
#
#   powershell -File windows-smoke.ps1 -Installer Daedalus-Setup-x64.exe
#
# It touches only the per-user folders the installer itself makes.
param([Parameter(Mandatory = $true)][string]$Installer)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$app = Join-Path $env:LOCALAPPDATA 'Programs\Daedalus'
$data = Join-Path $env:LOCALAPPDATA 'Daedalus\data'
$installer = (Resolve-Path $Installer).Path

function Fail([string]$message) { throw "windows-smoke: $message" }

function Running {
    @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($app, [StringComparison]::OrdinalIgnoreCase) })
}

function Install-Daedalus {
    $p = Start-Process -FilePath $installer -ArgumentList '/S' -Wait -PassThru
    if ($p.ExitCode -ne 0) { Fail "the installer exited with $($p.ExitCode)" }
    foreach ($file in 'Daedalus.exe', 'daedalus-desktop.exe', 'browserd.exe', 'miniapp-dist\index.html', 'Uninstall Daedalus.exe') {
        if (-not (Test-Path (Join-Path $app $file))) { Fail "the installation has no $file" }
    }
}

# The uninstaller copies itself out of the folder it removes and runs from there, so the process
# started here ends at once; the folder going away is what says it finished.
function Uninstall-Daedalus([string[]]$extra) {
    $uninstaller = Join-Path $app 'Uninstall Daedalus.exe'
    Start-Process -FilePath $uninstaller -ArgumentList (@('/currentuser', '/S') + $extra) -Wait | Out-Null
    for ($i = 0; $i -lt 120 -and (Test-Path (Join-Path $app 'Daedalus.exe')); $i++) { Start-Sleep 1 }
    if (Test-Path (Join-Path $app 'Daedalus.exe')) { Fail 'the uninstaller left the program behind' }
}

function Uninstall-Entry {
    Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' |
        ForEach-Object { Get-ItemProperty $_.PSPath } |
        Where-Object { $_.PSObject.Properties['UninstallString'] -and "$($_.UninstallString)".Contains($app) }
}

$desktopLink = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Daedalus.lnk'
$menuLink = Join-Path ([Environment]::GetFolderPath('Programs')) 'Daedalus.lnk'

Write-Host '--- install'
Install-Daedalus
$entry = Uninstall-Entry
if (-not $entry -or $entry.DisplayName -ne 'Daedalus') { Fail 'Apps & features has no Daedalus' }
Write-Host "Apps & features: $($entry.DisplayName) $($entry.DisplayVersion)"
if (-not (Test-Path $menuLink)) { Fail "no Start menu shortcut at $menuLink" }
if (-not (Test-Path $desktopLink)) { Fail "no desktop shortcut at $desktopLink" }
& (Join-Path $app 'daedalus-desktop.exe') --version
if ($LASTEXITCODE -ne 0) { Fail 'the launcher does not run' }

Write-Host '--- start'
$env:DAEDALUS_UPDATE_CHECK = 'off'
Start-Process -FilePath (Join-Path $app 'Daedalus.exe') -ArgumentList '--remote-debugging-port=9334' | Out-Null
$page = $null
for ($i = 0; $i -lt 90 -and -not $page; $i++) {
    Start-Sleep 1
    try {
        $page = @(Invoke-RestMethod -Uri 'http://127.0.0.1:9334/json/list' -TimeoutSec 2 | Where-Object { $_.type -eq 'page' -and $_.url -match '^http://127\.0\.0\.1:\d+/' } | ForEach-Object { $_.url }) | Select-Object -First 1
    } catch {}
}
if (-not $page) { Fail 'the window never showed the launcher''s page' }
Write-Host "the window shows $page"
$answer = Invoke-WebRequest -UseBasicParsing -Uri $page
if ($answer.Content -notmatch 'Daedalus') { Fail "$page does not answer as the launcher's page" }
if (-not (Test-Path (Join-Path $data 'launcher.json'))) { Fail "no launcher.json in ${data}: the data is not in the per-user folder" }
$engine = Running | Where-Object { $_.Name -eq 'daedalus-desktop.exe' }
if (-not $engine -or $engine.CommandLine -notmatch '--shell') { Fail 'the launcher is not running under the application' }

Write-Host '--- close'
$main = Running | Where-Object { $_.Name -eq 'Daedalus.exe' -and $_.CommandLine -notmatch '--type=' } | Select-Object -First 1
$process = Get-Process -Id $main.ProcessId
if ($process.MainWindowHandle -ne 0) {
    [void]$process.CloseMainWindow()
} else {
    # No desktop to have a main window on (a service session): the end of the window's process is
    # what the launcher must notice by itself.
    Stop-Process -Id $main.ProcessId
}
for ($i = 0; $i -lt 60 -and (Running).Count -gt 0; $i++) { Start-Sleep 1 }
if ((Running).Count -gt 0) { Fail "still running after the window closed: $((Running | ForEach-Object Name) -join ', ')" }
Write-Host "closed; nothing of the installation runs ($i s)"

Write-Host '--- uninstall, keeping the data'
Uninstall-Daedalus @()
if (-not (Test-Path $data)) { Fail 'the default uninstall deleted the data' }
if (Uninstall-Entry) { Fail 'Apps & features still lists Daedalus' }
if ((Test-Path $menuLink) -or (Test-Path $desktopLink)) { Fail 'a shortcut was left behind' }
Write-Host "removed; $data stays"

Write-Host '--- install again, uninstall deleting the data'
Install-Daedalus
Uninstall-Daedalus @('--remove-data')
if (Test-Path $data) { Fail 'the data was not deleted when that was asked for' }
Write-Host 'removed with the data'
