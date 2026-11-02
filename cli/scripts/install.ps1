# Installs daedalus-cli.exe and its short name, dae.exe, for the current user. dae.exe is a hard link
# to the same file, not a second build: whatever name it is started under, it is one program.
#
#   scripts\install.ps1 .\daedalus-cli.exe
param([Parameter(Mandatory = $true)][string]$Binary)
$ErrorActionPreference = "Stop"

$dest = Join-Path $env:LOCALAPPDATA "Programs\daedalus-cli"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
$target = Join-Path $dest "daedalus-cli.exe"
$short = Join-Path $dest "dae.exe"
Copy-Item -Force $Binary $target
if (Test-Path $short) { Remove-Item -Force $short }
New-Item -ItemType HardLink -Path $short -Target $target | Out-Null

$path = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not ($path -split ";" | Where-Object { $_ -eq $dest })) {
  [Environment]::SetEnvironmentVariable("Path", "$path;$dest", "User")
  Write-Host "added $dest to your PATH; open a new terminal to use it"
}
Write-Host "installed $target and $short"
