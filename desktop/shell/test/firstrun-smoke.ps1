# A first run from the seed the package carries (desktop/seed.go), on Windows: the same check as
# firstrun-smoke.sh, in PowerShell because the launcher quits when its standard input ends and a
# pipe from Git Bash's sleep to a Windows program is not closed when the sleep is killed — the bash
# version waited for a launcher that never heard the window close.
#
#   powershell -File firstrun-smoke.ps1 -Launcher dist\win-unpacked\daedalus-desktop.exe -Root C:\somewhere
#
# Root holds the data, the runtime and the state; nothing outside it is written: uv is told not to put
# python on PATH or in the registry, and the update check is off. The app's port is held by another
# program for the whole run, so the start has to move off it (desktop/ports.go) and the app must
# answer wherever the launcher wrote it went, with that program left alone.
param(
    [Parameter(Mandatory = $true)][string]$Launcher,
    [Parameter(Mandatory = $true)][string]$Root,
    [int]$Port = 18770,
    [int]$ApiPort = 18765,
    [int]$TimeoutSec = 600
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Fail([string]$message) { throw "firstrun-smoke: $message" }

$data = Join-Path $Root 'data'
$local = Join-Path $Root 'local'
New-Item -ItemType Directory -Force $data, $local, (Join-Path $data 'daedalus-secrets') | Out-Null
Set-Content -NoNewline -Encoding ascii (Join-Path $data 'mode') 'native'
Set-Content -NoNewline -Encoding ascii (Join-Path $data 'lang') 'en'
@"
API_PORT=$ApiPort
KEYPROXY_PORT=13201
DAEDALUS_SUPERVISOR_PORT=18769
SERVICES_PORT_RANGE=18100-18119
SERVICES_PUBLIC_HOST=127.0.0.1
USD_PER_DAY=20
"@ | Set-Content -Encoding ascii (Join-Path $data '.env')
'KEYPROXY_USD_PER_DAY=20' | Set-Content -Encoding ascii (Join-Path $data 'daedalus-secrets\keyproxy.env')

# Not Daedalus: a listener of this script's own on the port the env file names for the app.
$foreign = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $ApiPort)
$foreign.Start()
$envFile = Join-Path $data '.env'
function Current-ApiPort {
    $found = Select-String -Path $envFile -Pattern '^API_PORT=(\d+)' | Select-Object -Last 1
    if ($found) { return [int]$found.Matches[0].Groups[1].Value }
    return $ApiPort
}

$psi = New-Object Diagnostics.ProcessStartInfo
$psi.FileName = (Resolve-Path $Launcher).Path
$psi.Arguments = "--shell --data `"$data`" --port $Port --mode native start"
$psi.UseShellExecute = $false
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.CreateNoWindow = $true
$psi.EnvironmentVariables['DAEDALUS_LOCAL_ROOT'] = $local
$psi.EnvironmentVariables['DAEDALUS_UPDATE_CHECK'] = 'off'

$clock = [Diagnostics.Stopwatch]::StartNew()
$process = [Diagnostics.Process]::Start($psi)
# The launcher's events to the window: read so the pipe never fills, and otherwise not needed.
$events = $process.StandardOutput.ReadLineAsync()
$stage = $null
$ready = $null
$failure = $null
while ($clock.Elapsed.TotalSeconds -lt $TimeoutSec) {
    if ($events.IsCompleted) { $events = $process.StandardOutput.ReadLineAsync() }
    try {
        $status = Invoke-RestMethod -TimeoutSec 2 "http://127.0.0.1:$Port/api/status"
        if ($status.stage -ne $stage) { Write-Host ('{0,5:N0}s stage "{1}"' -f $clock.Elapsed.TotalSeconds, $status.stage); $stage = $status.stage }
        if ($status.failure -and -not $status.busy) { $failure = $status.failure; break }
    } catch {}
    $moved = Current-ApiPort
    try {
        if ($moved -ne $ApiPort -and (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:$moved/app/").StatusCode -eq 200) { $ready = $clock.Elapsed.TotalSeconds; break }
    } catch {}
    if ($process.HasExited) { $failure = "the launcher exited with $($process.ExitCode)"; break }
    Start-Sleep -Milliseconds 500
}
# Closing the window: the launcher stops the agent, then exits.
if (-not $process.HasExited) {
    $process.StandardInput.WriteLine('{"command":"quit"}')
    $process.StandardInput.Close()
    if (-not $process.WaitForExit(120000)) { $process.Kill(); Write-Host 'the launcher did not stop in 120 s' }
}

$logs = Get-ChildItem -Recurse -Path $local -Filter '*.log' | Where-Object { $_.DirectoryName -like '*logs' }
$log = $logs | Where-Object { $_.Name -eq 'launcher.log' } | Select-Object -First 1
if (-not $log) { Fail "no launcher.log under $local" }
$text = Get-Content -Raw $log.FullName
function Show-Logs {
    foreach ($one in $logs) { Write-Host "--- $($one.Name)"; Get-Content $one.FullName -Tail 40 | Write-Host }
}
if (-not $ready) {
    Show-Logs
    if ($failure) { Fail "the start failed: $failure" }
    Fail "the app never answered in $TimeoutSec s"
}
Write-Host ('the app answered after {0:N0}s on port {1}, moved off {2}' -f $ready, $moved, $ApiPort)
# The other program's listener is still this script's, untouched: it still holds the port.
$probe = New-Object Net.Sockets.TcpClient
try { $probe.Connect('127.0.0.1', $ApiPort) } catch { Fail "the program that held $ApiPort no longer listens" } finally { $probe.Close() }
$foreign.Stop()
if ($text -notmatch [regex]::Escape("the app's port $ApiPort is taken by another program; it moves to $moved")) { Show-Logs; Fail 'the log never says the app moved' }

foreach ($expected in 'uv .* comes with the installation', 'rg .* comes with the installation', 'git .* comes with the installation',
    'daedalus comes with the installation', 'protocore-exp comes with the installation',
    'installing python .* from the copy that came with the installation',
    'building the environment from the packages that came with the installation') {
    if ($text -notmatch $expected) { Show-Logs; Fail "the log never says: $expected" }
}
foreach ($refused in 'downloading ', 'fetching ', 'did not take python', 'were not enough', 'not usable') {
    if ($text -cmatch [regex]::Escape($refused)) { Show-Logs; Fail "the first run did this from the network: $refused" }
}
Write-Host 'everything came from the installation'
