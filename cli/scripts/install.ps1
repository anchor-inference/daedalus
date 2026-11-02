# Installs daedalus-cli.exe and its short name, dae.exe, for the current user. dae.exe is a hard link
# to the same file, not a second build: whatever name it is started under, it is one program.
#
#   irm https://raw.githubusercontent.com/anchor-inference/daedalus/main/cli/scripts/install.ps1 | iex
#
#   install.ps1                          the newest release's build for this machine (the default)
#   install.ps1 -Tag desktop-v0.23.0     that release's build instead
#   install.ps1 -FromSource              build from the checkout this script is in (needs Go)
#   install.ps1 -Binary .\daedalus-cli.exe   a binary already built or downloaded
#
# Under `irm | iex` there are no arguments; $env:DAEDALUS_CLI_TAG picks a release there.
#
# The client is released with the desktop application: every desktop-vX.Y.Z release carries
# daedalus-cli-windows-amd64.zip and daedalus-cli-windows-arm64.zip, listed in the release's
# SHA256SUMS. This picks the newest such release as desktop\install.ps1 does, checks the release's
# signature over its tag and SHA256SUMS by the project's release key, then the archive against
# SHA256SUMS, and only then unpacks it into %LOCALAPPDATA%\Programs\daedalus-cli and puts that folder
# on the user's PATH. The executable carries no Authenticode signature, so SmartScreen may warn.
#
# DAEDALUS_RELEASES_API and DAEDALUS_DOWNLOAD_BASE point it at another release source (a local
# fixture); it says so when they are set.
#
# This script has not been run on a real Windows machine yet; its signature check is
# desktop\install.ps1's, which has the same caveat.

# Nothing here calls `exit` under `irm | iex`, which would close the user's window together with the
# message explaining why: every failure is thrown, caught once at the bottom, printed, and left in
# $LASTEXITCODE; only a run as a script file ends with `exit`.

$ErrorActionPreference = 'Stop'

# The release keys this installer trusts, the same as desktop\install.ps1 and the launcher
# (desktop/signing.go, desktop/SIGNING.md): the project's release key, id df535393fa2485a1 (minisign
# shows it as A18524FA935353DF). Windows PowerShell has no Ed25519 of its own; the check uses
# OpenSSL 3 (on PATH, or the one Git for Windows ships) and without it the installer refuses.
$ReleaseKeys = @('RWTfU1OT+iSFoaxGzNfGzkwHdVs2o8WmnCzBUo/LBUw2L4ssGN4xYx/2')

function Fail([string]$message) {
    throw [System.InvalidOperationException]::new($message)
}

function Prop($object, [string]$name) {
    $property = $object.PSObject.Properties[$name]
    if ($property) { return $property.Value }
    return $null
}

# Test-ReleaseSignature checks SHA256SUMS.sig over "daedalus-release <tag>\n" + SHA256SUMS with one
# of $ReleaseKeys, through OpenSSL 3. Any doubt is a refusal. desktop\install.ps1 has the same.
function Test-ReleaseSignature([string]$tag, [string]$sums, [string]$sig, [string]$work) {
    if (-not (Test-Path $sig)) { Fail "$tag has no SHA256SUMS.sig, and this installer only installs signed releases. Nothing was installed." }
    # The first OpenSSL 3 found: an older one on PATH (1.1.1 is still common) has no -rawin.
    $candidates = @(Get-Command openssl -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
    if ($env:ProgramFiles) { $candidates += Join-Path $env:ProgramFiles 'Git\usr\bin\openssl.exe' }
    $opensslPath = $null
    foreach ($candidate in $candidates) {
        if (-not $candidate -or -not (Test-Path $candidate)) { continue }
        $saved = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $said = (& $candidate version 2>$null | Out-String)
        } catch {
            $said = ''
        } finally {
            $ErrorActionPreference = $saved
        }
        if ($said -match '^OpenSSL [3-9]\.') { $opensslPath = $candidate; break }
    }
    if (-not $opensslPath) { Fail 'Checking the release''s signature needs OpenSSL 3 (for example the one Git for Windows ships), which is not here. Nothing was installed.' }
    $message = Join-Path $work 'message'
    $header = [Text.Encoding]::ASCII.GetBytes("daedalus-release $tag`n")
    [IO.File]::WriteAllBytes($message, [byte[]]($header + [IO.File]::ReadAllBytes($sums)))
    $line = Get-Content $sig | Where-Object { $_ -and -not $_.StartsWith('untrusted comment:') } | Select-Object -First 1
    try { $raw = [Convert]::FromBase64String($line.Trim()) } catch { Fail 'SHA256SUMS.sig is not a signature. Nothing was installed.' }
    if ($raw.Length -ne 74 -or $raw[0] -ne 0x45 -or $raw[1] -ne 0x64) { Fail 'SHA256SUMS.sig is not an Ed25519 signature. Nothing was installed.' }
    $keyId = [BitConverter]::ToString($raw, 2, 8)
    $signature = Join-Path $work 'sig.bin'
    [IO.File]::WriteAllBytes($signature, [byte[]]$raw[10..73])
    foreach ($key in $ReleaseKeys) {
        $pub = [Convert]::FromBase64String($key)
        if ($pub.Length -ne 42 -or [BitConverter]::ToString($pub, 2, 8) -ne $keyId) { continue }
        $der = Join-Path $work 'key.der'
        [byte[]]$prefix = 0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00
        [IO.File]::WriteAllBytes($der, [byte[]]($prefix + $pub[10..41]))
        # Windows PowerShell 5.1 turns a native program's stderr into a terminating error under Stop;
        # OpenSSL's exit code, not its text, decides.
        $savedAction = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            & $opensslPath pkeyutl -verify -pubin -keyform DER -inkey $der -rawin -in $message -sigfile $signature *> $null
            $verifyExit = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $savedAction
        }
        if ($verifyExit -eq 0) {
            Write-Host "The release's signature verifies (key $keyId), for $tag."
            return
        }
        break
    }
    Fail "SHA256SUMS.sig does not verify with this installer's release key for $tag. Nothing was installed."
}

# Install-Binary puts the file in place under both names and the folder on the user's PATH.
function Install-Binary([string]$binary) {
    $dest = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Programs\daedalus-cli'
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    $target = Join-Path $dest 'daedalus-cli.exe'
    $short = Join-Path $dest 'dae.exe'
    # Both names go first: a hard link is the same file, so copying over one would rewrite the other
    # in place, and Windows refuses to replace a program that is running.
    foreach ($path in @($short, $target)) {
        if (Test-Path $path) {
            try { Remove-Item -Force $path } catch { Fail "$path is in use; close every daedalus-cli and dae and run this again." }
        }
    }
    Copy-Item -Force $binary $target
    New-Item -ItemType HardLink -Path $short -Target $target | Out-Null
    $path = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not ($path -split ';' | Where-Object { $_ -eq $dest })) {
        [Environment]::SetEnvironmentVariable('Path', "$path;$dest", 'User')
        Write-Host "added $dest to your PATH; open a new terminal to use it"
    }
    Write-Host "installed $target and $short"
    return 0
}

function Install-DaedalusCli([string[]]$arguments) {
    Set-StrictMode -Version 3
    $tag = $env:DAEDALUS_CLI_TAG
    $binary = $null
    $fromSource = $false
    for ($i = 0; $i -lt $arguments.Count; $i++) {
        switch ($arguments[$i]) {
            '-Tag' { $i++; if ($i -ge $arguments.Count) { Fail 'usage: install.ps1 [-Tag desktop-vX.Y.Z | -FromSource | -Binary <file>]' }; $tag = $arguments[$i] }
            '-Binary' { $i++; if ($i -ge $arguments.Count) { Fail 'usage: install.ps1 [-Tag desktop-vX.Y.Z | -FromSource | -Binary <file>]' }; $binary = $arguments[$i] }
            '-FromSource' { $fromSource = $true }
            default { Fail 'usage: install.ps1 [-Tag desktop-vX.Y.Z | -FromSource | -Binary <file>]' }
        }
    }

    if ($binary) {
        if (-not (Test-Path $binary -PathType Leaf)) { Fail "$binary is not a file." }
        return (Install-Binary (Resolve-Path $binary).Path)
    }

    $work = Join-Path ([IO.Path]::GetTempPath()) ("daedalus-cli-install-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $work | Out-Null
    try {
        if ($fromSource) {
            # The checkout this script is in; under `irm | iex` there is none.
            if (-not $PSScriptRoot) { Fail '-FromSource builds the checkout this script is in; run cli\scripts\install.ps1 from a clone.' }
            $source = Split-Path -Parent $PSScriptRoot
            if (-not (Test-Path (Join-Path $source 'cmd\daedalus-cli\main.go'))) { Fail "$source is not daedalus-cli's source folder." }
            if (-not (Get-Command go -ErrorAction SilentlyContinue)) { Fail 'Go is needed to build from source (https://go.dev/dl); or install a release without -FromSource.' }
            $out = Join-Path $work 'daedalus-cli.exe'
            $env:CGO_ENABLED = '0'
            Push-Location $source
            try {
                & go build -trimpath -ldflags '-s -w' -o $out ./cmd/daedalus-cli
                if ($LASTEXITCODE -ne 0) { Fail 'go build failed.' }
            } finally {
                Pop-Location
            }
            return (Install-Binary $out)
        }

        $repo = if ($env:DAEDALUS_REPO) { $env:DAEDALUS_REPO } else { 'anchor-inference/daedalus' }
        $api = if ($env:DAEDALUS_RELEASES_API) { $env:DAEDALUS_RELEASES_API.TrimEnd('/') } else { "https://api.github.com/repos/$repo" }
        $downloads = if ($env:DAEDALUS_DOWNLOAD_BASE) { $env:DAEDALUS_DOWNLOAD_BASE.TrimEnd('/') } else { "https://github.com/$repo/releases/download" }
        if ($env:DAEDALUS_RELEASES_API -or $env:DAEDALUS_DOWNLOAD_BASE) {
            Write-Host "Using a release source other than GitHub's: $api / $downloads"
        }
        # Windows PowerShell 5.1 may still default to TLS 1.0, which GitHub refuses.
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

        # The machine's own architecture, not this PowerShell's: an x64 PowerShell under emulation on
        # an ARM machine reports AMD64 in PROCESSOR_ARCHITECTURE.
        $arch = $env:PROCESSOR_ARCHITECTURE
        try { $arch = "$([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture)" } catch { }
        switch -regex ($arch) {
            '^(AMD64|X64)$' { $arch = 'amd64' }
            '^ARM64$' { $arch = 'arm64' }
            default { Fail "There is no build for $arch." }
        }
        $asset = "daedalus-cli-windows-$arch.zip"

        if ($tag) {
            if ($tag -notmatch '^desktop-v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$') { Fail "$tag is not a desktop-vX.Y.Z tag." }
        } else {
            Write-Host "Looking for the newest release of $repo..."
            $releases = Invoke-RestMethod -UseBasicParsing -Uri "$api/releases?per_page=30" -Headers @{ 'User-Agent' = 'daedalus-cli-install' }
            $release = @($releases) |
                Where-Object { -not (Prop $_ 'draft') -and -not (Prop $_ 'prerelease') -and "$(Prop $_ 'tag_name')" -match '^desktop-v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$' } |
                Sort-Object { [version]($_.tag_name -replace '^desktop-v', '') } -Descending |
                Select-Object -First 1
            if (-not $release) { Fail "No desktop-v* release found in $repo." }
            $tag = $release.tag_name
        }

        $zip = Join-Path $work $asset
        $sums = Join-Path $work 'SHA256SUMS'
        $sig = "$sums.sig"
        Write-Host "Downloading $asset from $tag..."
        try { Invoke-WebRequest -UseBasicParsing -Uri "$downloads/$tag/$asset" -OutFile $zip } catch { Fail "$tag has no $asset, or it could not be downloaded: $($_.Exception.Message)" }
        Invoke-WebRequest -UseBasicParsing -Uri "$downloads/$tag/SHA256SUMS" -OutFile $sums
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "$downloads/$tag/SHA256SUMS.sig" -OutFile $sig
        } catch {
            if (Test-Path $sig) { Remove-Item $sig }
        }
        $sha = [Security.Cryptography.SHA256]::Create()
        $fingerprint = -join ($sha.ComputeHash([Text.Encoding]::ASCII.GetBytes(($ReleaseKeys -join "`n"))) | ForEach-Object { $_.ToString('x2') })
        Write-Host "Release key fingerprint (SHA-256 of the key line): $fingerprint"
        Test-ReleaseSignature $tag $sums $sig $work

        $line = Get-Content $sums | Where-Object { $_ -match "^([0-9a-fA-F]{64})\s+\*?$([regex]::Escape($asset))$" } | Select-Object -First 1
        if (-not $line) { Fail "SHA256SUMS does not mention $asset. Nothing was installed." }
        $expected = ($line -split '\s+')[0].ToLowerInvariant()
        $stream = [IO.File]::OpenRead($zip)
        try {
            $actual = -join ($sha.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') })
        } finally {
            $stream.Dispose()
        }
        if ($actual -ne $expected) { Fail 'The download does not match its checksum. Nothing was installed.' }
        Write-Host 'Checksum matches.'

        # Only the one file is taken out, by its exact name: nothing in the archive is unpacked by a
        # path of its own choosing.
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [IO.Compression.ZipFile]::OpenRead($zip)
        $exe = Join-Path $work 'daedalus-cli.exe'
        try {
            $entry = $archive.Entries | Where-Object { $_.FullName -eq 'daedalus-cli.exe' } | Select-Object -First 1
            if (-not $entry) { Fail "$asset holds no daedalus-cli.exe. Nothing was installed." }
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $exe)
        } finally {
            $archive.Dispose()
        }
        return (Install-Binary $exe)
    } finally {
        Remove-Item -Recurse -Force -Path $work -ErrorAction SilentlyContinue
    }
}

$code = 1
try {
    # One array, not splatted: every argument reaches the function's own parsing.
    $code = @(Install-DaedalusCli ([string[]]@($args)))[-1]
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    $code = 1
}
$global:LASTEXITCODE = $code
if ($PSCommandPath) { exit $code }
