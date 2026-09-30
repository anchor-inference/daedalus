# Installs the Daedalus desktop launcher on Windows into a folder of its own - the same steps as
# install.sh, which covers macOS and Linux:
#
#   irm https://raw.githubusercontent.com/anchor-inference/daedalus/main/desktop/install.ps1 | iex
#
# It takes the newest desktop-vX.Y.Z release, downloads daedalus-desktop-windows-amd64.zip, checks it
# against the release's SHA256SUMS, refuses an archive with a path that would land outside the
# folder, and unpacks it into .\Daedalus (or $env:DAEDALUS_DIR).
#
# Run again over an installation that already has data, it never replaces anything itself: a
# launcher that has `daedalus-desktop.exe upgrade` is handed over to, and one too old for that
# (v0.12.0 and before) is upgraded by the launcher just downloaded and checked, as a bridge
# (`upgrade --bridge`) — the same yes, the same protection of the data (a checked backup, on
# Windows) with the old launcher's files kept aside before anything is replaced, and the same
# rollback.
#
# The release's signature over its tag and SHA256SUMS, by the project's release key, proves who
# published it, and the checksum catches a broken download. The executable itself carries no
# Authenticode signature, so SmartScreen warns on the first start.
#
# DAEDALUS_RELEASES_API and DAEDALUS_DOWNLOAD_BASE point it at another release source (a local
# fixture); it says so when they are set.
#
# This script has not yet been run on a real Windows machine (desktop/UPDATES.md).

# Under `irm ... | iex` this runs inside the user's own PowerShell session, so nothing here calls
# `exit`, which would close that window together with the message explaining why. Every failure is
# thrown, caught once at the bottom, printed, and left in $LASTEXITCODE; only a run as a script file
# (`powershell -File install.ps1`) ends with `exit`, where that is what the caller wants.

$ErrorActionPreference = 'Stop'

# The release keys this installer trusts (signify public keys, the base64 line), the same as the
# launcher's (desktop/signing.go, desktop/SIGNING.md): the project's release key, id df535393fa2485a1
# (minisign shows it as A18524FA935353DF). A release is installed only if SHA256SUMS.sig verifies —
# over "daedalus-release <tag>" and SHA256SUMS together — before anything from it is unpacked or run.
# Windows PowerShell has no Ed25519 of its own; the check uses OpenSSL 3 (on PATH, or the one Git for
# Windows ships) and without it the installer refuses. This script, arriving by `irm | iex` over TLS
# unsigned, cannot vouch for itself: compare the key fingerprint it prints with one published outside
# GitHub (SIGNING.md). None of this has been run on Windows yet.
$ReleaseKeys = @('RWTfU1OT+iSFoaxGzNfGzkwHdVs2o8WmnCzBUo/LBUw2L4ssGN4xYx/2')

function Fail([string]$message) {
    throw [System.InvalidOperationException]::new($message)
}

# Prop reads a property that may be missing, which Set-StrictMode would otherwise turn into an error.
function Prop($object, [string]$name) {
    $property = $object.PSObject.Properties[$name]
    if ($property) { return $property.Value }
    return $null
}

# ConvertTo-WindowsArgument quotes one argument so the program's C runtime (CommandLineToArgvW
# rules) reads it back exactly: backslashes before a quote, and at the end before the closing quote,
# are doubled — a bare "C:\" would otherwise turn the closing quote into a literal one. The same
# algorithm is checked in desktop/installer_ps_test.go against those parsing rules; this PowerShell
# text itself has not been run.
function ConvertTo-WindowsArgument([string]$argument) {
    if ($argument -ne '' -and $argument -notmatch '[\s"]') { return $argument }
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($c in $argument.ToCharArray()) {
        if ($c -eq [char]'\') { $slashes++; continue }
        if ($c -eq [char]'"') {
            [void]$builder.Append('\' * ($slashes * 2 + 1)).Append('"')
        } else {
            [void]$builder.Append('\' * $slashes).Append($c)
        }
        $slashes = 0
    }
    [void]$builder.Append('\' * ($slashes * 2)).Append('"')
    return $builder.ToString()
}

# Run-Launcher runs a launcher on this console — its question and its progress straight to the
# window, not through this function's output — waits for it and returns its exit code. Start-Process
# joins the arguments with bare spaces, so each is quoted by the rules above first.
function Run-Launcher([string]$exe, [string[]]$arguments) {
    $quoted = ($arguments | ForEach-Object { ConvertTo-WindowsArgument $_ }) -join ' '
    $process = Start-Process -FilePath $exe -ArgumentList $quoted -NoNewWindow -Wait -PassThru
    return $process.ExitCode
}

# Test-ReleaseSignature checks SHA256SUMS.sig over "daedalus-release <tag>\n" + SHA256SUMS with one
# of $ReleaseKeys, through OpenSSL 3. Any doubt is a refusal.
function Test-ReleaseSignature([string]$tag, [string]$sums, [string]$sig, [string]$work) {
    if (-not (Test-Path $sig)) { Fail "$tag has no SHA256SUMS.sig, and this installer only installs signed releases. Nothing was installed." }
    # The first OpenSSL 3 found: the one on PATH, then the one Git for Windows ships. An older OpenSSL
    # on PATH (1.1.1 is still common) has no -rawin, and taking it would refuse every good signature.
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
        # Windows PowerShell 5.1 promotes a native program's stderr to a terminating
        # error under Stop, even with redirection. OpenSSL writes failed verification
        # details there; its exit code, not that text, decides whether the key verifies.
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

function Install-Daedalus {
    Set-StrictMode -Version 3
    $repo = if ($env:DAEDALUS_REPO) { $env:DAEDALUS_REPO } else { 'anchor-inference/daedalus' }
    $dir = if ($env:DAEDALUS_DIR) { $env:DAEDALUS_DIR } else { Join-Path (Get-Location) 'Daedalus' }
    $api = if ($env:DAEDALUS_RELEASES_API) { $env:DAEDALUS_RELEASES_API.TrimEnd('/') } else { "https://api.github.com/repos/$repo" }
    $downloads = if ($env:DAEDALUS_DOWNLOAD_BASE) { $env:DAEDALUS_DOWNLOAD_BASE.TrimEnd('/') } else { "https://github.com/$repo/releases/download" }
    if ($env:DAEDALUS_RELEASES_API -or $env:DAEDALUS_DOWNLOAD_BASE) {
        Write-Host "Using a release source other than GitHub's: $api / $downloads"
    }

    # Windows PowerShell 5.1 may still default to TLS 1.0, which GitHub refuses.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $arch = $env:PROCESSOR_ARCHITECTURE
    if ($arch -ne 'AMD64' -and $arch -ne 'ARM64') { Fail "There is no build for $arch." }
    if ($arch -eq 'ARM64') { Write-Host 'There is no ARM64 build; the x86-64 one runs under emulation.' }
    $asset = 'daedalus-desktop-windows-amd64.zip'

    Write-Host "Looking for the newest desktop release of $repo..."
    $releases = Invoke-RestMethod -UseBasicParsing -Uri "$api/releases?per_page=30" -Headers @{ 'User-Agent' = 'daedalus-install' }
    $release = @($releases) |
        Where-Object { -not (Prop $_ 'draft') -and -not (Prop $_ 'prerelease') -and "$(Prop $_ 'tag_name')" -match '^desktop-v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$' } |
        Sort-Object { [version]($_.tag_name -replace '^desktop-v', '') } -Descending |
        Select-Object -First 1
    if (-not $release) { Fail "No desktop-v* release found in $repo." }
    $tag = $release.tag_name

    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $target = (Resolve-Path $dir).Path
    $launcher = Join-Path $target 'daedalus-desktop.exe'
    $data = Join-Path $target 'data'

    if ((Test-Path $data) -and (Test-Path $launcher)) {
        $help = & $launcher --help 2>$null | Out-String
        if ($help -match '(?m)^  upgrade ') {
            Write-Host "An installation with data is already in $target; handing over to its launcher's upgrade."
            return (Run-Launcher $launcher @('upgrade', '--data', $data))
        }
        $running = Get-Process -Name 'daedalus-desktop' -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -and $_.Path.StartsWith($target, [StringComparison]::OrdinalIgnoreCase) }
        if ($running) { Fail "The launcher in $target is running; close it and run this again. Nothing was changed." }
        # Too old for upgrade (v0.12.0 and before): the launcher downloaded below does it as a bridge,
        # after the same yes and the same checked backup of the data, with the old launcher's files kept aside.
        $bridgeNeeded = $true
    } elseif (Test-Path $data) {
        Fail "$data exists but there is no launcher beside it; not touching it."
    } else {
        $bridgeNeeded = $false
    }

    $work = Join-Path ([IO.Path]::GetTempPath()) ("daedalus-install-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $work | Out-Null
    try {
        $zip = Join-Path $work $asset
        $sums = Join-Path $work 'SHA256SUMS'
        Write-Host "Downloading $asset from $tag..."
        Invoke-WebRequest -UseBasicParsing -Uri "$downloads/$tag/$asset" -OutFile $zip
        Invoke-WebRequest -UseBasicParsing -Uri "$downloads/$tag/SHA256SUMS" -OutFile $sums
        # The signature: required, checked first, and left beside SHA256SUMS for the bridge.
        $sig = "$sums.sig"
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "$downloads/$tag/SHA256SUMS.sig" -OutFile $sig
        } catch {
            if (Test-Path $sig) { Remove-Item $sig }
            Fail "Could not download SHA256SUMS.sig; not installing an unverified release."
        }
        # Compare this with the fingerprint published outside GitHub (SIGNING.md, the README).
        $sha = [Security.Cryptography.SHA256]::Create()
        $fingerprint = -join ($sha.ComputeHash([Text.Encoding]::ASCII.GetBytes(($ReleaseKeys -join "`n"))) | ForEach-Object { $_.ToString('x2') })
        Write-Host "Release key fingerprint (SHA-256 of the key line): $fingerprint"
        Test-ReleaseSignature $tag $sums $sig $work

        $line = Get-Content $sums | Where-Object { $_ -match "^([0-9a-fA-F]{64})\s+\*?$([regex]::Escape($asset))$" } | Select-Object -First 1
        if (-not $line) { Fail "SHA256SUMS does not mention $asset." }
        $expected = ($line -split '\s+')[0].ToLowerInvariant()
        # .NET rather than Get-FileHash: Windows PowerShell started from a pwsh 7 inherits a module path
        # its own Utility module does not load from, and there Get-FileHash did not exist at all.
        $stream = [IO.File]::OpenRead($zip)
        try {
            $actual = -join ($sha.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') })
        } finally {
            $stream.Dispose()
        }
        if ($actual -ne $expected) { Fail 'The download does not match its checksum; not installing it.' }
        Write-Host 'Checksum matches.'

        # Every entry is checked before anything is unpacked: a name that is rooted, has a drive, or
        # climbs out with .. is refused, whichever PowerShell version would otherwise unpack it.
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [IO.Compression.ZipFile]::OpenRead($zip)
        try {
            foreach ($entry in $archive.Entries) {
                $name = $entry.FullName -replace '\\', '/'
                if ($name.StartsWith('/') -or $name.Contains(':') -or ($name -split '/') -contains '..') {
                    Fail "The archive holds '$($entry.FullName)', which is not a plain relative path; not installing it."
                }
            }
        } finally {
            $archive.Dispose()
        }
        $staged = Join-Path $work 'new'
        # The same reason as the hash above: Expand-Archive lives in a script module that the same
        # module path can hide, and every entry was already checked against climbing out.
        [IO.Compression.ZipFile]::ExtractToDirectory($zip, $staged)
        if (-not (Test-Path (Join-Path $staged 'daedalus-desktop.exe'))) { Fail 'The archive has no daedalus-desktop.exe.' }

        if ($bridgeNeeded) {
            # This script replaces nothing over an installation with data. The new launcher does it, and
            # changes nothing when it cannot back up first (Docker mode, for now) or is not told yes.
            Write-Host "An installation with data is already in $target, under a launcher that predates upgrade."
            Write-Host "The $tag launcher will upgrade it, keeping the data and the launcher from before so that a failure puts both back."
            $bridgeArgs = @('upgrade', '--bridge', '--root', $target, '--data', $data, '--archive', $zip, '--sums', $sums)
            if ($env:DAEDALUS_UPGRADE_YES -eq '1') { $bridgeArgs += '--yes' }
            return (Run-Launcher (Join-Path $staged 'daedalus-desktop.exe') $bridgeArgs)
        }

        # A launcher with no data beside it has never been run: nothing to back up. Its files are still
        # moved aside, not deleted.
        if (Test-Path $launcher) {
            $kept = Join-Path $target (".daedalus-upgrade\installer-" + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ'))
            New-Item -ItemType Directory -Force -Path $kept | Out-Null
            foreach ($item in 'daedalus-desktop.exe', 'ptyd.exe', 'browserd.exe', 'miniapp-dist') {
                $path = Join-Path $target $item
                if (Test-Path $path) { Move-Item -Path $path -Destination $kept }
            }
            Write-Host "The previous launcher's files are in $kept."
        }
        Get-ChildItem -Path $staged | ForEach-Object { Move-Item -Path $_.FullName -Destination $target }
    } finally {
        Remove-Item -Recurse -Force -Path $work -ErrorAction SilentlyContinue
    }

    Write-Host ''
    Write-Host "Installed $tag into $target."
    Write-Host "Run it:  & '$launcher'"
    Write-Host 'The executable is not signed, so SmartScreen warns once: More info, then Run anyway.'
    Write-Host 'The launcher makes the checkouts, the keys and the data inside that folder, and keeps its downloaded'
    Write-Host 'runtime and local state in %LOCALAPPDATA%\Daedalus; `daedalus-desktop.exe uninstall` removes those.'
        return 0
}

$code = 1
try {
    # The last value is the status; anything else a step let slip into the output is not.
    $code = @(Install-Daedalus)[-1]
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    $code = 1
}
$global:LASTEXITCODE = $code
if ($PSCommandPath) { exit $code }
