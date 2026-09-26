# Canonical public-installer tester for Okto Neuron on Windows PowerShell.
#
# Runs the raw GitHub install.ps1 entrypoint in an isolated PowerShell process so
# the test cannot modify the caller's real ~/.okto-neuron or ~/.marginalia vaults or Claude config.

[CmdletBinding()]
param(
    [string]$InstallUrl = $(if ($env:OKTO_NEURON_INSTALL_URL) { $env:OKTO_NEURON_INSTALL_URL } else { "https://raw.githubusercontent.com/OktoLabsAI/okto-neuron/main/install.ps1" }),
    [string]$TestHome = $(if ($env:OKTO_NEURON_TEST_HOME) { $env:OKTO_NEURON_TEST_HOME } else { Join-Path ([IO.Path]::GetTempPath()) ("okto-neuron-install-test-" + [guid]::NewGuid().ToString("N").Substring(0, 12)) }),
    [string]$Vault = $(if ($env:OKTO_NEURON_VAULT) { $env:OKTO_NEURON_VAULT } else { "mynotes" }),
    [string]$ExpectedVersion = $(if ($env:OKTO_NEURON_EXPECTED_VERSION) { $env:OKTO_NEURON_EXPECTED_VERSION } else { "0.3.0" }),
    [ValidateSet("interactive", "skip", "custom", "release-lifecycle")]
    [string]$Profile = "interactive",
    [string]$ApiBase = "",
    [string]$Model = "",
    [string]$DriverCommit = $(if ($env:OKTO_NEURON_TEST_DRIVER_COMMIT) { $env:OKTO_NEURON_TEST_DRIVER_COMMIT } else { "" }),
    [switch]$NoServe,
    [switch]$Cleanup,
    [switch]$AllowExistingDaemon
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Die([string]$Message) {
    Write-Error "error: $Message"
    exit 1
}

# Windows PowerShell 5.1 turns each redirected stderr line of a native command
# into an ErrorRecord; under $ErrorActionPreference = "Stop" the first one is a
# terminating NativeCommandError even when the command exits 0. Run native
# commands with Continue scoped to this function and leave $LASTEXITCODE to the
# caller. A command that cannot be found still throws.
function Invoke-TestNative([string]$Command, [object[]]$Arguments = @(), [switch]$MergeStderr) {
    Get-Command -Name $Command -ErrorAction Stop | Out-Null
    $ErrorActionPreference = "Continue"
    if ($MergeStderr) {
        & $Command @Arguments 2>&1 | ForEach-Object { "$_" }
    } else {
        & $Command @Arguments 2>$null
    }
}

function Test-PortOpen([int]$Port) {
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $iar = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(500, $false)) {
            return $false
        }
        $client.EndConnect($iar)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Test-ProcessAlive([int]$ProcessId) {
    try {
        Get-Process -Id $ProcessId -ErrorAction Stop | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Remove-DirectoryTree([string]$Path) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return }
    $fullPath = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $extendedPath = if ($fullPath.StartsWith("\\", [StringComparison]::Ordinal)) {
        "\\?\UNC\" + $fullPath.Substring(2)
    } else {
        "\\?\$fullPath"
    }
    [IO.Directory]::Delete($extendedPath, $true)
    if (Test-Path -LiteralPath $fullPath) {
        throw "could not remove directory tree: $fullPath"
    }
}

function Read-TestServerProcessId([string]$Path) {
    try {
        $record = Get-Item -LiteralPath $Path -ErrorAction Stop
        if ($record.Length -gt 16384) { return 0 }
        $raw = (Get-Content -Raw -LiteralPath $Path -ErrorAction Stop).Trim()
    } catch {
        return 0
    }
    if (-not $raw) { return 0 }
    $recordProcessId = 0
    if ([int]::TryParse($raw, [ref]$recordProcessId)) {
        if ($recordProcessId -gt 0) { return $recordProcessId }
        return 0
    }
    try {
        $payload = ConvertFrom-Json $raw -ErrorAction Stop
    } catch {
        return 0
    }
    if ($null -eq $payload -or -not ($payload.PSObject.Properties.Name -contains "pid")) {
        return 0
    }
    $recordProcessId = 0
    if (-not ([int]::TryParse([string]$payload.pid, [ref]$recordProcessId))) { return 0 }
    if ($recordProcessId -gt 0) { return $recordProcessId }
    return 0
}

function Stop-TestDaemon([string]$HomePath, [string]$VaultName, [string]$ToolBin) {
    $recordPath = Join-Path (Join-Path (Join-Path $HomePath ".okto-neuron") "runtime") ".marginalia\server.pid"
    $recordProcessId = Read-TestServerProcessId $recordPath
    $daemonPresent = (
        ($recordProcessId -gt 0 -and (Test-ProcessAlive $recordProcessId)) -or
        (Test-PortOpen 7777) -or
        (Test-PortOpen 8201)
    )
    if (-not $daemonPresent) { return }

    $sandboxCli = $null
    foreach ($name in @("okto-neuron.exe", "okto-neuron.cmd", "okto-neuron", "marginalia.exe", "marginalia.cmd", "marginalia")) {
        $candidate = Join-Path $ToolBin $name
        if (Test-Path -LiteralPath $candidate) {
            $sandboxCli = $candidate
            break
        }
    }
    if (-not $sandboxCli) {
        throw "cleanup found a sandbox daemon but no sandbox okto-neuron or marginalia command; retained $HomePath"
    }
    Invoke-TestNative $sandboxCli @("stop", "--timeout", "30") | Out-Null

    for ($i = 0; $i -lt 60; $i++) {
        if ($recordProcessId -le 0) {
            $recordProcessId = Read-TestServerProcessId $recordPath
        }
        $processAlive = $recordProcessId -gt 0 -and (Test-ProcessAlive $recordProcessId)
        if (-not $processAlive -and -not (Test-PortOpen 7777) -and -not (Test-PortOpen 8201)) {
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw "cleanup could not verify sandbox daemon shutdown; retained $HomePath"
}

function Initialize-TestSandbox([string]$HomePath, [string]$TempRoot) {
    $resolvedHome = [IO.Path]::GetFullPath($HomePath).TrimEnd('\', '/')
    $resolvedTemp = [IO.Path]::GetFullPath($TempRoot).TrimEnd('\', '/')
    $parent = [IO.Path]::GetFullPath((Split-Path -Parent $resolvedHome)).TrimEnd('\', '/')
    $leaf = Split-Path -Leaf $resolvedHome
    if ($parent -ne $resolvedTemp -or $leaf -notmatch '^okto-neuron-install-test-[A-Za-z0-9._-]+$') {
        throw "test home must be a uniquely named okto-neuron-install-test-* directory directly under $resolvedTemp"
    }
    $marker = Join-Path $resolvedHome ".okto-neuron-test-sandbox-owner"
    if (Test-Path -LiteralPath $resolvedHome) {
        if (-not (Test-Path -LiteralPath $marker -PathType Leaf) -or
            (Get-Content -Raw -LiteralPath $marker).Trim() -ne "okto-neuron-test-sandbox-v1") {
            throw "refusing to reuse unowned test directory: $resolvedHome"
        }
    } else {
        New-Item -ItemType Directory -Path $resolvedHome -ErrorAction Stop | Out-Null
        Set-Content -LiteralPath $marker -Value "okto-neuron-test-sandbox-v1" -Encoding ASCII
    }
    return $resolvedHome
}

function Remove-TestSandbox([string]$HomePath, [string]$TempRoot) {
    $resolvedHome = [IO.Path]::GetFullPath($HomePath).TrimEnd('\', '/')
    $resolvedTemp = [IO.Path]::GetFullPath($TempRoot).TrimEnd('\', '/')
    $parent = [IO.Path]::GetFullPath((Split-Path -Parent $resolvedHome)).TrimEnd('\', '/')
    $marker = Join-Path $resolvedHome ".okto-neuron-test-sandbox-owner"
    if ($parent -ne $resolvedTemp -or
        -not (Test-Path -LiteralPath $marker -PathType Leaf) -or
        (Get-Content -Raw -LiteralPath $marker).Trim() -ne "okto-neuron-test-sandbox-v1") {
        throw "refusing to delete unowned test directory: $resolvedHome"
    }
    Remove-DirectoryTree $resolvedHome
}

# Every spelling of a Windows path that tools print: C:\Users\name,
# C:/Users/name (file:/// URLs, Python), C:\\Users\\name (JSON) and the
# percent-encoded forms (%3A, %5C, %20). Longest first, so a longer spelling is
# replaced before a shorter one it contains.
function Get-PublicPathVariants([string]$Path) {
    if (-not $Path) { return @() }
    $back = $Path.Replace('/', '\').TrimEnd('\')
    if (-not $back) { return @() }
    $forward = $back.Replace('\', '/')
    $variants = @(
        $back,
        $forward,
        $back.Replace('\', '\\'),
        $forward.Replace(' ', '%20'),
        $forward.Replace(':', '%3A').Replace(' ', '%20'),
        $back.Replace('\', '%5C').Replace(' ', '%20'),
        $back.Replace(':', '%3A').Replace('\', '%5C').Replace(' ', '%20')
    )
    return @($variants | Select-Object -Unique | Sort-Object -Property { $_.Length } -Descending)
}

function Export-PublicEvidence(
    [string]$RawPath,
    [string]$PublicPath,
    [string]$TestRoot,
    [string]$CallerHome,
    [string]$CallerIdentity,
    [string]$MachineIdentity
) {
    if (-not (Test-Path -LiteralPath $RawPath -PathType Leaf)) {
        throw "raw Windows transcript was not created: $RawPath"
    }
    $lines = @(Get-Content -LiteralPath $RawPath)
    $delimiters = @()
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^\*{10,}$') { $delimiters += $i }
    }
    if ($delimiters.Count -lt 4) {
        throw "raw Windows transcript did not contain complete start/end headers"
    }
    $bodyStart = $delimiters[1] + 1
    $bodyEnd = $delimiters[$delimiters.Count - 2] - 1
    if ($bodyEnd -lt $bodyStart) {
        throw "raw Windows transcript contained no evidence body"
    }
    $text = ($lines[$bodyStart..$bodyEnd] -join "`r`n") + "`r`n"
    # The test root sits under the caller's home, so it is replaced first.
    $replacements = @()
    foreach ($variant in (Get-PublicPathVariants $TestRoot)) {
        $replacements += [pscustomobject]@{ From = $variant; To = "<TEST_HOME>" }
    }
    foreach ($variant in (Get-PublicPathVariants $CallerHome)) {
        $replacements += [pscustomobject]@{ From = $variant; To = "<CALLER_HOME>" }
    }
    $replacements += [pscustomobject]@{ From = $CallerIdentity; To = "<WINDOWS_USER>" }
    $replacements += [pscustomobject]@{ From = $MachineIdentity; To = "<WINDOWS_MACHINE>" }
    foreach ($replacement in $replacements) {
        if ($replacement.From) {
            $text = [regex]::Replace(
                $text,
                [regex]::Escape([string]$replacement.From),
                [string]$replacement.To,
                [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
            )
        }
    }
    foreach ($forbidden in @(
        'Windows PowerShell transcript (?:start|end)',
        '(?m)^(?:Username|RunAs User|Machine|Host Application|Process ID|PSVersion|PSEdition|PSCompatibleVersions|BuildVersion|CLRVersion|WSManStackVersion|PSRemotingProtocolVersion|SerializationVersion):'
    )) {
        if ($text -match $forbidden) {
            throw "sanitized Windows evidence retained a transcript identity/header field"
        }
    }
    # Fail closed: no spelling of a private path or identity may survive, not
    # even one a console wrapped across two lines.
    $unwrapped = $text -replace "\r?\n", ""
    $private = @(Get-PublicPathVariants $TestRoot) + @(Get-PublicPathVariants $CallerHome) +
        @($CallerIdentity, $MachineIdentity)
    foreach ($identity in $private) {
        if (-not $identity) { continue }
        foreach ($candidate in @($text, $unwrapped)) {
            if ($candidate.IndexOf($identity, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                throw "sanitized Windows evidence retained private identity data"
            }
        }
    }
    Set-Content -LiteralPath $PublicPath -Value $text -Encoding UTF8
}

if ($Profile -eq "custom" -and (-not $ApiBase -or -not $Model)) {
    Die "-Profile custom requires -ApiBase and -Model"
}
if ($Profile -eq "release-lifecycle" -and $NoServe) {
    Die "-Profile release-lifecycle cannot be combined with -NoServe"
}
if ($Profile -eq "release-lifecycle" -and
    ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or
     $PSVersionTable.PSEdition -ne "Desktop")) {
    Die "-Profile release-lifecycle requires Windows PowerShell 5.1 on Windows"
}

if (-not $AllowExistingDaemon) {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:7777/health" -TimeoutSec 2 -ErrorAction Stop | Out-Null
        Die "127.0.0.1:7777 already has an Okto Neuron daemon. Stop it before running the isolated test."
    } catch {
        if (Test-PortOpen 7777) {
            Die "127.0.0.1:7777 is already in use. Stop the process before running the isolated test."
        }
    }
    if (-not $NoServe -and (Test-PortOpen 8201)) {
        Die "127.0.0.1:8201 is already in use. Stop the process before running the isolated test."
    }
}

$OriginalTempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$TestHome = Initialize-TestSandbox $TestHome $OriginalTempRoot
$RealHome = [IO.Path]::GetFullPath($HOME)
if ($TestHome -eq $RealHome -or
    $TestHome.StartsWith((Join-Path $RealHome ".okto-neuron"), [StringComparison]::OrdinalIgnoreCase) -or
    $TestHome.StartsWith((Join-Path $RealHome ".marginalia"), [StringComparison]::OrdinalIgnoreCase)) {
    Die "refusing to use your real HOME, ~/.okto-neuron or ~/.marginalia as the test home"
}
$SandboxUvToolDir = Join-Path $TestHome ".uv\tools"
$SandboxUvToolBin = Join-Path $TestHome ".uv\bin"
$SandboxUvPythonDir = Join-Path $TestHome ".uv\python"
$SandboxUvCacheDir = Join-Path $TestHome ".uv\cache"

New-Item -ItemType Directory -Force -Path `
    $TestHome, `
    $SandboxUvToolDir, `
    $SandboxUvToolBin, `
    $SandboxUvPythonDir, `
    $SandboxUvCacheDir | Out-Null
$Runner = Join-Path $TestHome "windows-runner.ps1"
$EvidenceStem = "evidence-" + (Get-Date -Format "yyyyMMdd-HHmmss")
$RawEvidence = Join-Path $TestHome ($EvidenceStem + ".private.raw.log")
$Evidence = Join-Path $TestHome ($EvidenceStem + ".public.log")
$DriverUrl = "UNPINNED_SMOKE"
$DriverSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $PSCommandPath).Hash.ToLowerInvariant()
$InstallSha256 = ""
$ManifestUrl = ""
$ManifestSha256 = ""
if ($Profile -eq "release-lifecycle") {
    if ($DriverCommit -cnotmatch '^[0-9a-f]{40}$') {
        throw "-DriverCommit must be an exact 40-character lowercase dist commit for release-lifecycle"
    }
    $rawBase = "https://raw.githubusercontent.com/OktoLabsAI/okto-neuron/$DriverCommit"
    $DriverUrl = "$rawBase/bin/test-install.ps1"
    $InstallUrl = "$rawBase/install.ps1"
    $ManifestUrl = "$rawBase/release-manifest.json"
    $remoteDriver = Join-Path $TestHome "provenance-test-install.ps1"
    $remoteInstaller = Join-Path $TestHome "provenance-install.ps1"
    $remoteManifest = Join-Path $TestHome "provenance-release-manifest.json"
    Invoke-WebRequest -UseBasicParsing -Uri $DriverUrl -OutFile $remoteDriver
    Invoke-WebRequest -UseBasicParsing -Uri $InstallUrl -OutFile $remoteInstaller
    Invoke-WebRequest -UseBasicParsing -Uri $ManifestUrl -OutFile $remoteManifest
    $remoteDriverHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $remoteDriver).Hash.ToLowerInvariant()
    if ($remoteDriverHash -ne $DriverSha256) {
        throw "running test-install.ps1 does not match exact public driver commit $DriverCommit"
    }
    $InstallSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $remoteInstaller).Hash.ToLowerInvariant()
    $ManifestSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $remoteManifest).Hash.ToLowerInvariant()
    $manifest = Get-Content -Raw -LiteralPath $remoteManifest | ConvertFrom-Json
    if ($ExpectedVersion -and [string]$manifest.version -ne $ExpectedVersion) {
        throw "pinned release manifest version '$($manifest.version)' does not match $ExpectedVersion"
    }
    $ExpectedVersion = [string]$manifest.version
} elseif (-not $ExpectedVersion) {
    throw "OKTO_NEURON_EXPECTED_VERSION is required by this unbaked tester"
}

$child = @'
param(
    [string]$InstallUrl,
    [string]$TestHome,
    [string]$Vault,
    [string]$ExpectedVersion,
    [string]$Profile,
    [string]$ApiBase,
    [string]$Model,
    [string]$DriverCommit,
    [string]$DriverUrl,
    [string]$DriverSha256,
    [string]$InstallSha256,
    [string]$ManifestUrl,
    [string]$ManifestSha256,
    [switch]$NoServe,
    [string]$Evidence
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# Windows PowerShell 5.1 turns each redirected stderr line of a native command
# into an ErrorRecord; under $ErrorActionPreference = "Stop" the first one is a
# terminating NativeCommandError even when the command exits 0. Run native
# commands with Continue scoped to this function and leave $LASTEXITCODE to the
# caller. A command that cannot be found still throws.
function Invoke-TestNative([string]$Command, [object[]]$Arguments = @(), [switch]$MergeStderr) {
    Get-Command -Name $Command -ErrorAction Stop | Out-Null
    $ErrorActionPreference = "Continue"
    if ($MergeStderr) {
        & $Command @Arguments 2>&1 | ForEach-Object { "$_" }
    } else {
        & $Command @Arguments 2>$null
    }
}

New-Item -ItemType Directory -Force -Path `
    $TestHome, `
    (Join-Path $TestHome "AppData\Roaming"), `
    (Join-Path $TestHome "AppData\Local"), `
    (Join-Path $TestHome ".cache"), `
    (Join-Path $TestHome ".config"), `
    (Join-Path $TestHome ".local\share"), `
    (Join-Path $TestHome ".uv\tools"), `
    (Join-Path $TestHome ".uv\bin"), `
    (Join-Path $TestHome ".uv\python"), `
    (Join-Path $TestHome ".uv\python-bin"), `
    (Join-Path $TestHome ".uv\python-cache"), `
    (Join-Path $TestHome ".uv\cache"), `
    (Join-Path $TestHome ".local\bin"), `
    (Join-Path $TestHome "tmp") | Out-Null

$resolvedHome = [IO.Path]::GetFullPath($HOME)
$resolvedExpected = [IO.Path]::GetFullPath($TestHome)
if ($resolvedHome -ne $resolvedExpected) {
    throw "PowerShell HOME isolation failed: HOME is '$resolvedHome', expected '$resolvedExpected'. Launch the tester from test-install.ps1, not the runner directly."
}

if ($Profile -eq "release-lifecycle") {
    Remove-Item Env:OKTO_NEURON_VAULT -ErrorAction SilentlyContinue
} else {
    $env:OKTO_NEURON_VAULT = $Vault
}
$env:OKTO_NEURON_EXPECTED_VERSION = $ExpectedVersion
$env:OKTO_NEURON_NO_MCP = "1"
$env:OKTO_NEURON_NO_OPEN = "1"
# This tester redirects HOME/USERPROFILE but not the real Windows user-PATH
# registry - never let a sandbox run persist PATH there via uv tool update-shell.
$env:OKTO_NEURON_NO_UPDATE_SHELL = "1"
$env:UV_TOOL_DIR = Join-Path $TestHome ".uv\tools"
$env:UV_TOOL_BIN_DIR = Join-Path $TestHome ".uv\bin"
$env:UV_INSTALL_DIR = Join-Path $TestHome ".uv\bin"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $TestHome ".uv\python"
$env:UV_PYTHON_BIN_DIR = Join-Path $TestHome ".uv\python-bin"
$env:UV_PYTHON_CACHE_DIR = Join-Path $TestHome ".uv\python-cache"
$env:UV_CACHE_DIR = Join-Path $TestHome ".uv\cache"
$env:UV_NO_MODIFY_PATH = "1"
$env:UV_PYTHON_NO_REGISTRY = "1"
$env:UV_PYTHON_INSTALL_REGISTRY = "0"
Remove-Item Env:UV_UNMANAGED_INSTALL -ErrorAction SilentlyContinue
$env:TEMP = Join-Path $TestHome "tmp"
$env:TMP = Join-Path $TestHome "tmp"
$env:XDG_DATA_HOME = Join-Path $TestHome ".local\share"
$env:XDG_CACHE_HOME = Join-Path $TestHome ".cache"
$env:XDG_CONFIG_HOME = Join-Path $TestHome ".config"
$env:XDG_BIN_HOME = Join-Path $TestHome ".local\bin"
if ($NoServe) {
    $env:OKTO_NEURON_NO_SERVE = "1"
}

foreach ($name in @(
    "OKTO_NEURON_ONBOARD_NONINTERACTIVE",
    "OKTO_NEURON_LLM_PROVIDER",
    "OKTO_NEURON_LLM_API_BASE",
    "OKTO_NEURON_LLM_API_KEY_ENV",
    "OKTO_NEURON_LLM_MODEL",
    "OKTO_NEURON_LLM_SKIP_DISCOVERY",
    "OKTO_NEURON_LLM_ALLOW_REMOTE",
    "OKTO_NEURON_ALLOW_REMOTE_LLM",
    "OKTO_NEURON_PACKS",
    "OKTO_NEURON_WHEEL",
    "OKTO_NEURON_SRC",
    "OKTO_NEURON_MANIFEST",
    "OKTO_NEURON_WHEEL_SHA256",
    "OKTO_NEURON_DEFAULT_WHEEL_URL",
    "OKTO_NEURON_DEFAULT_MANIFEST_URL",
    "OKTO_NEURON_REPO",
    "OKTO_NEURON_REF",
    "OKTO_NEURON_ENDPOINT",
    "OKTO_NEURON_AUTH_TOKEN",
    "OKTO_NEURON_TEST_INSTALL_URL",
    "OKTO_NEURON_TEST_FAIL_ACTIVATION",
    "OKTO_NEURON_REAL_UV"
)) {
    if (Test-Path (Join-Path "Env:" $name)) {
        Remove-Item (Join-Path "Env:" $name) -Force
    }
}

if ($Profile -eq "skip") {
    $env:OKTO_NEURON_ONBOARD_NONINTERACTIVE = "1"
    $env:OKTO_NEURON_LLM_PROVIDER = "skip"
} elseif ($Profile -eq "custom") {
    $env:OKTO_NEURON_ONBOARD_NONINTERACTIVE = "1"
    $env:OKTO_NEURON_LLM_PROVIDER = "custom"
    $env:OKTO_NEURON_LLM_API_BASE = $ApiBase
    $env:OKTO_NEURON_LLM_MODEL = $Model
}
if ($Profile -eq "release-lifecycle") {
    $env:OKTO_NEURON_MANIFEST = $ManifestUrl
    $env:OKTO_NEURON_DEFAULT_MANIFEST_URL = $ManifestUrl
    $existingVaultConfig = Join-Path `
        (Join-Path (Join-Path (Join-Path $HOME ".okto-neuron") "vaults") $Vault) `
        "okto-neuron.yaml"
    $existingTool = Join-Path $env:UV_TOOL_DIR "okto-neuron"
    if ((Test-Path -LiteralPath $existingVaultConfig) -or
        (Test-Path -LiteralPath $existingTool)) {
        throw "release-lifecycle requires a fresh test home: $TestHome"
    }
}

function Test-ChildProcessAlive([int]$ProcessId) {
    try {
        Get-Process -Id $ProcessId -ErrorAction Stop | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Read-ChildServerProcessId([string]$Path) {
    try {
        $record = Get-Item -LiteralPath $Path -ErrorAction Stop
        if ($record.Length -gt 16384) { return 0 }
        $raw = (Get-Content -Raw -LiteralPath $Path -ErrorAction Stop).Trim()
    } catch {
        return 0
    }
    if (-not $raw) { return 0 }
    $recordProcessId = 0
    if ([int]::TryParse($raw, [ref]$recordProcessId)) {
        if ($recordProcessId -gt 0) { return $recordProcessId }
        return 0
    }
    try {
        $payload = ConvertFrom-Json $raw -ErrorAction Stop
    } catch {
        return 0
    }
    if ($null -eq $payload -or -not ($payload.PSObject.Properties.Name -contains "pid")) {
        return 0
    }
    $recordProcessId = 0
    if (-not ([int]::TryParse([string]$payload.pid, [ref]$recordProcessId))) { return 0 }
    if ($recordProcessId -gt 0) { return $recordProcessId }
    return 0
}

function Test-ChildTcpPort([int]$Port) {
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $result = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $result.AsyncWaitHandle.WaitOne(500, $false)) {
            return $false
        }
        $client.EndConnect($result)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Wait-TestHealth([string]$Endpoint) {
    for ($i = 0; $i -lt 90; $i++) {
        try {
            Invoke-RestMethod -Uri "$($Endpoint.TrimEnd('/'))/health" -TimeoutSec 2 -ErrorAction Stop | Out-Null
            return
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    throw "daemon did not become healthy at $Endpoint"
}

function Wait-TestDaemonStopped([int]$ProcessId, [int[]]$Ports) {
    for ($i = 0; $i -lt 60; $i++) {
        $processAlive = $ProcessId -gt 0 -and (Test-ChildProcessAlive $ProcessId)
        $portOpen = $false
        foreach ($port in $Ports) {
            if (Test-ChildTcpPort $port) {
                $portOpen = $true
                break
            }
        }
        if (-not $processAlive -and -not $portOpen) {
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw "daemon process or ports remained live after stop (pid $ProcessId; ports $($Ports -join ','))"
}

function Invoke-TestCli([string]$Command, [string[]]$Arguments) {
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "okto-neuron $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Get-TestStatus([string]$Command, [string]$Endpoint = "") {
    $oldEndpoint = $env:OKTO_NEURON_ENDPOINT
    try {
        if ($Endpoint) {
            $env:OKTO_NEURON_ENDPOINT = $Endpoint
        } else {
            Remove-Item Env:OKTO_NEURON_ENDPOINT -ErrorAction SilentlyContinue
        }
        $raw = (Invoke-TestNative $Command @("status", "--json", "--timeout", "5") | Out-String)
        if ($LASTEXITCODE -ne 0 -or -not $raw.Trim()) {
            throw "application status failed"
        }
        return ($raw | ConvertFrom-Json)
    } finally {
        $env:OKTO_NEURON_ENDPOINT = $oldEndpoint
    }
}

function Confirm-DefaultDaemon([string]$Command, [string]$Version) {
    $endpoint = "http://127.0.0.1:7777"
    Wait-TestHealth $endpoint
    $status = Get-TestStatus $Command
    if ([string]$status.okto_neuron_version -ne $Version) {
        throw "server version '$($status.okto_neuron_version)' does not match $Version"
    }
    if ([string]$status.endpoint -ne $endpoint) {
        throw "server endpoint '$($status.endpoint)' does not match $endpoint"
    }
    $processId = [int]$status.pid
    if ($processId -le 0 -or -not (Test-ChildProcessAlive $processId)) {
        throw "application status did not identify a live daemon process"
    }

    $response = Invoke-WebRequest -UseBasicParsing -Uri "$endpoint/" `
        -TimeoutSec 5 -ErrorAction Stop
    if ($response.StatusCode -ne 200 -or $response.Content -notmatch '(?i)<(?:!doctype\s+html|html)') {
        throw "plain UI fetch did not return the Okto Neuron HTML application"
    }

    $uiOutput = (Invoke-TestNative $Command @("ui", "--no-open") | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "okto-neuron ui --no-open failed"
    }
    $expectedUi = "Okto Neuron UI is ready at $endpoint/; browser launch skipped"
    if ($uiOutput -ne $expectedUi) {
        throw "unexpected UI output: $uiOutput"
    }
    Write-Host $uiOutput
    return $status
}

function Invoke-RawInstallerProcess([string]$Source, [string]$OutputPath) {
    $nestedPowerShell = Join-Path $PSHOME "powershell.exe"
    $installCommand = '$ProgressPreference = ''SilentlyContinue''; $source = $env:OKTO_NEURON_TEST_INSTALL_URL; if (Test-Path -LiteralPath $source) { Get-Content -Raw -LiteralPath $source | Invoke-Expression } else { Invoke-RestMethod -UseBasicParsing $source | Invoke-Expression }'
    $oldInstallUrl = $env:OKTO_NEURON_TEST_INSTALL_URL
    $oldErrorActionPreference = $ErrorActionPreference
    try {
        $env:OKTO_NEURON_TEST_INSTALL_URL = $Source
        $ErrorActionPreference = "Continue"
        $output = @(& $nestedPowerShell -NoProfile -ExecutionPolicy Bypass -Command $installCommand 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldErrorActionPreference
        $env:OKTO_NEURON_TEST_INSTALL_URL = $oldInstallUrl
    }
    $output | Set-Content -LiteralPath $OutputPath -Encoding UTF8
    $output | Out-Host
    return $exitCode
}

function Assert-OutputContains([string]$Path, [string]$Expected) {
    $output = Get-Content -Raw -LiteralPath $Path
    if (-not $output.Contains($Expected)) {
        throw "installer output $Path did not contain: $Expected"
    }
}

function Assert-TokenUnchanged([string]$Path, [string]$Baseline) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "daemon token disappeared during release lifecycle"
    }
    $current = [Convert]::ToBase64String([IO.File]::ReadAllBytes($Path))
    if ($current -cne $Baseline) {
        throw "daemon token changed during release lifecycle"
    }
}

function Assert-CanonicalTestPidRecord(
    [string]$Python,
    [string]$HelperPath,
    [string]$RecordPath,
    [int]$ExpectedProcessId
) {
    $validationOutput = @(
        Invoke-TestNative $Python @($HelperPath, "validate", $RecordPath, [string]$ExpectedProcessId) -MergeStderr
    )
    $validationExitCode = $LASTEXITCODE
    $validationText = (($validationOutput | ForEach-Object { [string]$_ }) -join "`n").Trim()
    if ($validationExitCode -ne 0) {
        throw "canonical application PID record validation failed: $validationText"
    }
    if ($validationText -notmatch '^[0-9a-f]{32}$') {
        throw "canonical application PID record returned an invalid owner identity: $validationText"
    }
    return $validationText
}

function Invoke-StoppedPredecessorUpdate(
    [string]$Url,
    [string]$Version,
    [string]$SuccessorManifestUrl,
    [string]$HomePath
) {
    # Predecessor: Marginalia 0.2.0 (the last release under the pre-0.3.0 name),
    # installed stopped by the exact marginalia-dist installer of that release.
    $predecessorDistCommit = "29683d2156840eadaff43efd6133750cd7f24465"
    $predecessorInstallUrl = "https://raw.githubusercontent.com/OktoLabsAI/marginalia-dist/$predecessorDistCommit/install.ps1"
    $predecessorInstallSha = "3207bcbee4b738491afd617bcb10e0fa583a6e6af41df51cfd999459f23e7327"
    $predecessorManifestUrl = "https://raw.githubusercontent.com/OktoLabsAI/marginalia-dist/$predecessorDistCommit/release-manifest.json"
    $predecessorManifestSha = "22eb2f07ee453a708052469d71382a2dc47b0491ff8534d7899325f3bea8068f"
    $predecessorWheelSha = "59adee4483d8f85525e30a5e6271e6328238ac275d4b3fe05e29325d2ed29520"
    $successorWheelUrl = "https://github.com/OktoLabsAI/okto-neuron/releases/download/v$Version/okto_neuron-$Version-py3-none-any.whl"
    $predecessorHome = Join-Path $HomePath "stopped-predecessor"
    $predecessorToolBin = Join-Path $predecessorHome ".uv\bin"
    $pidRoot = Join-Path $predecessorHome ".marginalia"
    $newPidRoot = Join-Path $predecessorHome ".okto-neuron"
    $predecessorInstall = Join-Path $predecessorHome "predecessor-v020-install.ps1"
    $predecessorManifest = Join-Path $predecessorHome "predecessor-v020-manifest.json"
    $predecessorOutput = Join-Path $predecessorHome "predecessor-v020-install.out"
    $successorOutput = Join-Path $predecessorHome "successor-stopped-update.out"
    $predecessorCli = $null
    $environmentNames = @(
        "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "PATH",
        "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_BIN_HOME",
        "UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_INSTALL_DIR", "UV_PYTHON_INSTALL_DIR",
        "UV_PYTHON_BIN_DIR", "UV_PYTHON_CACHE_DIR", "UV_CACHE_DIR",
        "MARGINALIA_EXPECTED_VERSION", "MARGINALIA_MANIFEST",
        "MARGINALIA_DEFAULT_MANIFEST_URL", "MARGINALIA_NO_SERVE", "MARGINALIA_NO_UPDATE_SHELL",
        "OKTO_NEURON_EXPECTED_VERSION", "OKTO_NEURON_MANIFEST",
        "OKTO_NEURON_DEFAULT_MANIFEST_URL", "OKTO_NEURON_DEFAULT_WHEEL_URL"
    )
    $savedEnvironment = @{}
    foreach ($name in $environmentNames) {
        $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    }

    try {
        New-Item -ItemType Directory -Force -Path `
            $predecessorHome, `
            (Join-Path $predecessorHome "AppData\Roaming"), `
            (Join-Path $predecessorHome "AppData\Local"), `
            (Join-Path $predecessorHome ".cache"), `
            (Join-Path $predecessorHome ".config"), `
            (Join-Path $predecessorHome ".local\share"), `
            (Join-Path $predecessorHome ".local\bin"), `
            (Join-Path $predecessorHome ".uv\tools"), `
            $predecessorToolBin, `
            (Join-Path $predecessorHome ".uv\python"), `
            (Join-Path $predecessorHome ".uv\python-bin"), `
            (Join-Path $predecessorHome ".uv\python-cache"), `
            (Join-Path $predecessorHome ".uv\cache"), `
            (Join-Path $predecessorHome "tmp") | Out-Null

        $env:HOME = $predecessorHome
        $env:USERPROFILE = $predecessorHome
        $env:APPDATA = Join-Path $predecessorHome "AppData\Roaming"
        $env:LOCALAPPDATA = Join-Path $predecessorHome "AppData\Local"
        $env:TEMP = Join-Path $predecessorHome "tmp"
        $env:TMP = Join-Path $predecessorHome "tmp"
        $env:XDG_DATA_HOME = Join-Path $predecessorHome ".local\share"
        $env:XDG_CACHE_HOME = Join-Path $predecessorHome ".cache"
        $env:XDG_CONFIG_HOME = Join-Path $predecessorHome ".config"
        $env:XDG_BIN_HOME = Join-Path $predecessorHome ".local\bin"
        $env:UV_TOOL_DIR = Join-Path $predecessorHome ".uv\tools"
        $env:UV_TOOL_BIN_DIR = $predecessorToolBin
        $env:UV_INSTALL_DIR = $predecessorToolBin
        $env:UV_PYTHON_INSTALL_DIR = Join-Path $predecessorHome ".uv\python"
        $env:UV_PYTHON_BIN_DIR = Join-Path $predecessorHome ".uv\python-bin"
        $env:UV_PYTHON_CACHE_DIR = Join-Path $predecessorHome ".uv\python-cache"
        $env:UV_CACHE_DIR = Join-Path $predecessorHome ".uv\cache"
        $env:Path = "$predecessorToolBin;$($savedEnvironment['PATH'])"

        Invoke-WebRequest -UseBasicParsing -Uri $predecessorManifestUrl -OutFile $predecessorManifest
        $actualManifestSha = (
            Get-FileHash -Algorithm SHA256 -LiteralPath $predecessorManifest
        ).Hash.ToLowerInvariant()
        if ($actualManifestSha -ne $predecessorManifestSha) {
            throw "immutable v0.2.0 manifest SHA-256 mismatch"
        }
        $manifest = Get-Content -Raw -LiteralPath $predecessorManifest | ConvertFrom-Json
        if ([string]$manifest.version -ne "0.2.0" -or
            [string]$manifest.sha256 -ne $predecessorWheelSha) {
            throw "immutable v0.2.0 manifest identity mismatch"
        }
        Invoke-WebRequest -UseBasicParsing -Uri $predecessorInstallUrl -OutFile $predecessorInstall
        $actualInstallSha = (
            Get-FileHash -Algorithm SHA256 -LiteralPath $predecessorInstall
        ).Hash.ToLowerInvariant()
        if ($actualInstallSha -ne $predecessorInstallSha) {
            throw "immutable v0.2.0 installer SHA-256 mismatch"
        }

        Write-Host "PREDECESSOR_DIST_COMMIT=$predecessorDistCommit"
        Write-Host "PREDECESSOR_MANIFEST_URL=$predecessorManifestUrl"
        Write-Host "PREDECESSOR_MANIFEST_SHA256=$predecessorManifestSha"
        Write-Host "PREDECESSOR_WHEEL_SHA256=$predecessorWheelSha"
        Write-Host "PREDECESSOR_INSTALL_URL=$predecessorInstallUrl"
        Write-Host "PREDECESSOR_INSTALL_SHA256=$predecessorInstallSha"
        Write-Host "PREDECESSOR_BOOTSTRAP_MODE=pinned-marginalia-dist-v0.2.0-installer-stopped"

        # The 0.2.0 installer reads only the old names.
        $env:MARGINALIA_EXPECTED_VERSION = "0.2.0"
        $env:MARGINALIA_MANIFEST = $predecessorManifestUrl
        $env:MARGINALIA_DEFAULT_MANIFEST_URL = $predecessorManifestUrl
        $env:MARGINALIA_NO_SERVE = "1"
        # The 0.2.0 installer persists PATH through `uv tool update-shell` into the
        # real Windows user-PATH registry, which HOME redirection does not isolate.
        $env:MARGINALIA_NO_UPDATE_SHELL = "1"
        if ((Invoke-RawInstallerProcess $predecessorInstall $predecessorOutput) -ne 0) {
            throw "stopped v0.2.0 predecessor installation failed"
        }
        Remove-Item Env:MARGINALIA_EXPECTED_VERSION, Env:MARGINALIA_MANIFEST, `
            Env:MARGINALIA_DEFAULT_MANIFEST_URL, Env:MARGINALIA_NO_UPDATE_SHELL -ErrorAction SilentlyContinue

        foreach ($name in @("marginalia.exe", "marginalia.cmd", "marginalia")) {
            $candidate = Join-Path $predecessorToolBin $name
            if (Test-Path -LiteralPath $candidate) {
                $predecessorCli = $candidate
                break
            }
        }
        if (-not $predecessorCli) {
            throw "stopped v0.2.0 predecessor CLI was not installed"
        }
        $predecessorVersion = ([string](
            & $predecessorCli --version | Select-Object -First 1
        )).Trim()
        if ($predecessorVersion -ne "marginalia 0.2.0") {
            throw "predecessor CLI version '$predecessorVersion' does not match 0.2.0"
        }

        $pidRecords = @()
        if (Test-Path -LiteralPath $pidRoot) {
            $pidRecords = @(Get-ChildItem -LiteralPath $pidRoot -Filter "server.pid" -Recurse -File)
        }
        if ($pidRecords.Count -ne 0 -or
            (Test-ChildTcpPort 7777) -or
            (Test-ChildTcpPort 8201)) {
            throw "v0.2.0 predecessor was not stopped before update"
        }

        $env:OKTO_NEURON_EXPECTED_VERSION = $Version
        $env:OKTO_NEURON_MANIFEST = $SuccessorManifestUrl
        $env:OKTO_NEURON_DEFAULT_MANIFEST_URL = $SuccessorManifestUrl
        $env:OKTO_NEURON_DEFAULT_WHEEL_URL = $successorWheelUrl
        if ((Invoke-RawInstallerProcess $Url $successorOutput) -ne 0) {
            throw "stopped update from Marginalia v0.2.0 to Okto Neuron v$Version failed"
        }

        $successorCli = Join-Path $predecessorToolBin "okto-neuron.exe"
        if (-not (Test-Path -LiteralPath $successorCli)) {
            throw "stopped predecessor update did not install the okto-neuron command"
        }
        $successorVersion = ([string](
            & $successorCli --version | Select-Object -First 1
        )).Trim()
        if ($successorVersion -ne "okto-neuron $Version") {
            throw "stopped predecessor update left CLI version '$successorVersion'"
        }
        $aliasVersion = ([string](
            Invoke-TestNative $predecessorCli @("--version") | Select-Object -First 1
        )).Trim()
        if ($aliasVersion -ne "okto-neuron $Version") {
            throw "the marginalia alias reports '$aliasVersion' after the update"
        }
        $pidRecords = @()
        foreach ($root in @($pidRoot, $newPidRoot)) {
            if (Test-Path -LiteralPath $root) {
                $pidRecords += @(Get-ChildItem -LiteralPath $root -Filter "server.pid" -Recurse -File)
            }
        }
        if ($pidRecords.Count -ne 0 -or
            (Test-ChildTcpPort 7777) -or
            (Test-ChildTcpPort 8201)) {
            throw "stopped predecessor update unexpectedly started a daemon"
        }
        Write-Host "WINDOWS_RELEASE_LIFECYCLE_PREDECESSOR_STOPPED_UPDATE_OK"
    } finally {
        $cleanupFailure = ""
        try {
            if (-not $predecessorCli) {
                foreach ($name in @("marginalia.exe", "marginalia.cmd", "marginalia")) {
                    $candidate = Join-Path $predecessorToolBin $name
                    if (Test-Path -LiteralPath $candidate) {
                        $predecessorCli = $candidate
                        break
                    }
                }
            }
            $pidRecords = @()
            if (Test-Path -LiteralPath $pidRoot) {
                $pidRecords = @(
                    Get-ChildItem -LiteralPath $pidRoot -Filter "server.pid" -Recurse -File
                )
            }
            $daemonPresent = (
                $pidRecords.Count -ne 0 -or
                (Test-ChildTcpPort 7777) -or
                (Test-ChildTcpPort 8201)
            )
            if ($daemonPresent) {
                if (-not $predecessorCli) {
                    $cleanupFailure = "nested predecessor daemon has no sandbox CLI"
                } else {
                    Invoke-TestNative $predecessorCli @("stop", "--timeout", "30") | Out-Null
                    if ($LASTEXITCODE -ne 0) {
                        $cleanupFailure = "nested predecessor daemon stop failed"
                    }
                    for ($i = 0; $i -lt 60; $i++) {
                        $pidRecords = @()
                        if (Test-Path -LiteralPath $pidRoot) {
                            $pidRecords = @(
                                Get-ChildItem -LiteralPath $pidRoot `
                                    -Filter "server.pid" -Recurse -File
                            )
                        }
                        if ($pidRecords.Count -eq 0 -and
                            -not (Test-ChildTcpPort 7777) -and
                            -not (Test-ChildTcpPort 8201)) {
                            break
                        }
                        Start-Sleep -Milliseconds 500
                    }
                    if ($pidRecords.Count -ne 0 -or
                        (Test-ChildTcpPort 7777) -or
                        (Test-ChildTcpPort 8201)) {
                        $cleanupFailure = "nested predecessor daemon remained live after cleanup"
                    }
                }
            }
        } finally {
            foreach ($name in $environmentNames) {
                [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], "Process")
            }
        }
        if ($cleanupFailure) {
            throw $cleanupFailure
        }
    }
}

function Invoke-ReleaseLifecycle(
    [string]$Command,
    [string]$VaultPath,
    [string]$Version,
    [string]$Url,
    [string]$HomePath
) {
    $status = Confirm-DefaultDaemon $Command $Version
    if ([int]$status.vault_count -ne 0 -or (Test-Path -LiteralPath $VaultPath)) {
        throw "default installer did not start as an application-first, no-vault daemon"
    }
    Write-Host "WINDOWS_RELEASE_LIFECYCLE_APP_FIRST_OK"
    Invoke-TestCli $Command @("vault", "create", (Split-Path -Leaf $VaultPath), "--use")
    $status = Confirm-DefaultDaemon $Command $Version
    if ([int]$status.vault_count -ne 1) {
        throw "created vault was not discovered by the application daemon"
    }
    Write-Host "RELEASE_LIFECYCLE_FRESH_INSTALL_OK"
    Write-Host "RELEASE_LIFECYCLE_STATUS_UI_OK"

    $yaml = Join-Path $VaultPath "okto-neuron.yaml"
    $configHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $yaml).Hash.ToLowerInvariant()
    $tokenPath = Join-Path (Join-Path $HomePath ".okto-neuron") "daemon-7777.token"
    $initialToken = [Convert]::ToBase64String([IO.File]::ReadAllBytes($tokenPath))

    $stoppedProcessId = [int]$status.pid
    Invoke-TestCli $Command @("stop", "--timeout", "30")
    Wait-TestDaemonStopped $stoppedProcessId @(7777, 8201)
    $stoppedOutput = Join-Path $HomePath "stopped-update.out"
    $stoppedExitCode = Invoke-RawInstallerProcess $Url $stoppedOutput
    if ($stoppedExitCode -ne 0) {
        throw "stopped update failed with exit code $stoppedExitCode"
    }
    Assert-OutputContains $stoppedOutput "daemon remains stopped"
    if ((Test-ChildTcpPort 7777) -or (Test-ChildTcpPort 8201)) {
        throw "stopped update unexpectedly started the daemon"
    }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $yaml).Hash.ToLowerInvariant() -ne $configHash) {
        throw "stopped update changed okto-neuron.yaml"
    }
    Assert-TokenUnchanged $tokenPath $initialToken
    Write-Host "RELEASE_LIFECYCLE_STOPPED_UPDATE_OK"

    Invoke-TestCli $Command @("serve", "--daemon", "--no-open")
    $status = Confirm-DefaultDaemon $Command $Version
    $runningBefore = [int]$status.pid
    $runningOutput = Join-Path $HomePath "running-update.out"
    $runningExitCode = Invoke-RawInstallerProcess $Url $runningOutput
    if ($runningExitCode -ne 0) {
        throw "running update failed with exit code $runningExitCode"
    }
    $status = Confirm-DefaultDaemon $Command $Version
    $runningAfter = [int]$status.pid
    if ($runningBefore -eq $runningAfter) {
        throw "running update did not replace the daemon process"
    }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $yaml).Hash.ToLowerInvariant() -ne $configHash) {
        throw "running update changed okto-neuron.yaml"
    }
    Assert-TokenUnchanged $tokenPath $initialToken
    $realUv = (Get-Command uv -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $toolRoot = [IO.Path]::GetFullPath($env:UV_TOOL_DIR)
    if (-not (Test-Path -LiteralPath $toolRoot -PathType Container)) {
        throw "uv tool directory disappeared after running update: $toolRoot"
    }
    $rollbackSentinel = Join-Path (Join-Path $toolRoot "okto-neuron") `
        ".release-lifecycle-previous-tool-sentinel"
    if (Test-Path -LiteralPath $rollbackSentinel) {
        throw "previous-tool rollback sentinel already exists: $rollbackSentinel"
    }
    Set-Content -LiteralPath $rollbackSentinel `
        -Value "previous-tool-only:$Version" -Encoding ASCII
    $rollbackSentinelHash = (Get-FileHash -Algorithm SHA256 `
        -LiteralPath $rollbackSentinel).Hash.ToLowerInvariant()
    Write-Host "RELEASE_LIFECYCLE_RUNNING_UPDATE_OK"

    Invoke-TestCli $Command @("stop", "--timeout", "30")
    Wait-TestDaemonStopped $runningAfter @(7777, 8201)
    Invoke-TestCli $Command @(
        "serve", "--daemon", "--no-open", "--port", "7788", "--mcp-port", "8202"
    )
    $customEndpoint = "http://127.0.0.1:7788"
    Wait-TestHealth $customEndpoint
    $customStatus = Get-TestStatus $Command $customEndpoint
    $customProcessId = [int]$customStatus.pid
    if ($customProcessId -le 0 -or -not (Test-ChildProcessAlive $customProcessId)) {
        throw "custom-port status did not identify a live daemon process"
    }
    if ([string]$customStatus.endpoint -ne $customEndpoint -or
        [string]$customStatus.okto_neuron_version -ne $Version) {
        throw "custom-port status did not report the expected endpoint and version"
    }
    $customOutput = Join-Path $HomePath "custom-port-refusal.out"
    $oldEndpoint = $env:OKTO_NEURON_ENDPOINT
    try {
        $env:OKTO_NEURON_ENDPOINT = $customEndpoint
        $customExitCode = Invoke-RawInstallerProcess $Url $customOutput
    } finally {
        $env:OKTO_NEURON_ENDPOINT = $oldEndpoint
    }
    if ($customExitCode -eq 0) {
        throw "installer accepted a running custom-port daemon"
    }
    Assert-OutputContains $customOutput "uses custom endpoint"
    Assert-OutputContains $customOutput $customEndpoint
    if (-not (Test-ChildProcessAlive $customProcessId)) {
        throw "custom-port refusal stopped the existing daemon"
    }
    if (([string](& $Command --version | Select-Object -First 1)).Trim() -ne "okto-neuron $Version") {
        throw "custom-port refusal changed the installed CLI version"
    }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $yaml).Hash.ToLowerInvariant() -ne $configHash) {
        throw "custom-port refusal changed okto-neuron.yaml"
    }
    Assert-TokenUnchanged $tokenPath $initialToken
    if (-not (Test-Path -LiteralPath $rollbackSentinel -PathType Leaf)) {
        throw "custom-port refusal replaced the previous tool"
    }
    if ((Get-FileHash -Algorithm SHA256 `
        -LiteralPath $rollbackSentinel).Hash.ToLowerInvariant() -ne $rollbackSentinelHash) {
        throw "custom-port refusal changed the previous tool"
    }
    try {
        $env:OKTO_NEURON_ENDPOINT = $customEndpoint
        Invoke-TestCli $Command @("stop", "--timeout", "30")
    } finally {
        $env:OKTO_NEURON_ENDPOINT = $oldEndpoint
    }
    Wait-TestDaemonStopped $customProcessId @(7788, 8202)
    Write-Host "RELEASE_LIFECYCLE_CUSTOM_PORT_REFUSAL_OK"

    $guardRuntimeRoot = Join-Path (Join-Path $HomePath ".okto-neuron") "runtime"
    $guardRecordRoot = Join-Path $guardRuntimeRoot ".marginalia"
    $guardRecordPath = Join-Path $guardRecordRoot "server.pid"
    $guardHelperName = "okto-neuron-pid-owner.py"
    $guardHelperPath = Join-Path $HomePath $guardHelperName
    $guardReadyPath = Join-Path $HomePath "okto-neuron-pid-owner.ready"
    $guardStopPath = Join-Path $HomePath "okto-neuron-pid-owner.stop"
    $guardPython = Join-Path (Join-Path $toolRoot "okto-neuron") "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $guardPython -PathType Leaf)) {
        throw "installed successor Python was not found: $guardPython"
    }
    New-Item -ItemType Directory -Force -Path $guardRecordRoot | Out-Null
    if (Test-Path -LiteralPath $guardRecordPath) {
        throw "custom-port daemon left an application PID record: $guardRecordPath"
    }
    Remove-Item -LiteralPath $guardHelperPath, $guardReadyPath, $guardStopPath `
        -Force -ErrorAction SilentlyContinue

    $guardHelperSource = @"
import json
import os
import signal
import string
import sys
import threading
from pathlib import Path

from okto_neuron.server import lifecycle


def hold() -> None:
    root = Path(os.environ["OKTO_NEURON_TEST_PID_ROOT"])
    ready_path = Path(os.environ["OKTO_NEURON_TEST_PID_READY"])
    stop_path = Path(os.environ["OKTO_NEURON_TEST_PID_STOP"])
    stopping = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    with lifecycle.PidFile(root):
        temporary = ready_path.with_name(f".{ready_path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(f"{os.getpid()}\n", encoding="ascii")
            os.replace(temporary, ready_path)
        finally:
            temporary.unlink(missing_ok=True)
        while not stopping.wait(0.1):
            if stop_path.exists():
                break


def validate(record_path: Path, expected_pid: int) -> None:
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    if set(payload) != {"version", "pid", "start_token", "owner_id"}:
        raise SystemExit("PID record fields are not canonical")
    if payload["version"] != lifecycle.PID_RECORD_VERSION:
        raise SystemExit("PID record version is not canonical")
    if payload["pid"] != expected_pid:
        raise SystemExit("PID record does not identify the helper process")
    current_token = lifecycle._process_start_token(expected_pid)
    if current_token is None or payload["start_token"] != current_token:
        raise SystemExit("PID record birth token does not identify the helper process")
    owner_id = payload["owner_id"]
    if not isinstance(owner_id, str) or len(owner_id) != 32:
        raise SystemExit("PID record owner identity is not canonical")
    if any(character not in string.hexdigits for character in owner_id):
        raise SystemExit("PID record owner identity is not hexadecimal")
    print(owner_id, flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "hold":
        hold()
    elif len(sys.argv) == 4 and sys.argv[1] == "validate":
        validate(Path(sys.argv[2]), int(sys.argv[3]))
    else:
        raise SystemExit("usage: okto-neuron-pid-owner.py hold | validate RECORD PID")
"@
    [IO.File]::WriteAllText($guardHelperPath, $guardHelperSource, [Text.Encoding]::ASCII)

    $guardStartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $guardStartInfo.FileName = $guardPython
    $guardStartInfo.WorkingDirectory = $HomePath
    # Windows PowerShell 5.1 has no ProcessStartInfo.ArgumentList. Both command-line
    # tokens are fixed ASCII; every variable path travels through a property or a
    # child-only environment variable, avoiding command-line quoting ambiguity.
    $guardStartInfo.Arguments = "$guardHelperName hold"
    $guardStartInfo.UseShellExecute = $false
    $guardStartInfo.CreateNoWindow = $true
    $guardStartInfo.RedirectStandardOutput = $true
    $guardStartInfo.RedirectStandardError = $true
    $guardStartInfo.EnvironmentVariables["OKTO_NEURON_TEST_PID_ROOT"] = $guardRuntimeRoot
    $guardStartInfo.EnvironmentVariables["OKTO_NEURON_TEST_PID_READY"] = $guardReadyPath
    $guardStartInfo.EnvironmentVariables["OKTO_NEURON_TEST_PID_STOP"] = $guardStopPath

    $guardProcess = New-Object System.Diagnostics.Process
    $guardProcess.StartInfo = $guardStartInfo
    $guardStarted = $false
    $guardCleanupFailure = ""
    $guardForced = $false
    try {
        if (-not $guardProcess.Start()) {
            throw "installed successor PID owner process did not start"
        }
        $guardStarted = $true
        $guardReady = $false
        for ($i = 0; $i -lt 100; $i++) {
            if ((Test-Path -LiteralPath $guardReadyPath -PathType Leaf) -and
                (Test-Path -LiteralPath $guardRecordPath -PathType Leaf)) {
                $guardReady = $true
                break
            }
            if ($guardProcess.HasExited) {
                $guardError = $guardProcess.StandardError.ReadToEnd().Trim()
                throw "installed successor PID owner exited before readiness: $guardError"
            }
            Start-Sleep -Milliseconds 100
        }
        if (-not $guardReady) {
            throw "installed successor PID owner did not become ready"
        }
        $guardReadyProcessId = 0
        $guardReadyText = (Get-Content -Raw -LiteralPath $guardReadyPath).Trim()
        if (-not ([int]::TryParse($guardReadyText, [ref]$guardReadyProcessId)) -or
            $guardReadyProcessId -ne $guardProcess.Id) {
            throw "installed successor PID owner readiness did not match process $($guardProcess.Id)"
        }
        $guardOwnerId = Assert-CanonicalTestPidRecord `
            $guardPython $guardHelperPath $guardRecordPath $guardProcess.Id
        $guardOutput = Join-Path $HomePath "live-pid-refusal.out"
        $guardExitCode = Invoke-RawInstallerProcess $Url $guardOutput
        if ($guardExitCode -eq 0) {
            throw "installer replaced the tool despite a verified application PID owner"
        }
        Assert-OutputContains $guardOutput "live Okto Neuron process (pid $($guardProcess.Id))"
        Assert-OutputContains $guardOutput "okto-neuron stop"
        if (-not (Test-ChildProcessAlive $guardProcess.Id)) {
            throw "live-PID refusal stopped the lifecycle owner process"
        }
        $guardOwnerIdAfter = Assert-CanonicalTestPidRecord `
            $guardPython $guardHelperPath $guardRecordPath $guardProcess.Id
        if ($guardOwnerIdAfter -cne $guardOwnerId) {
            throw "live-PID refusal changed the lifecycle owner identity"
        }
        if (([string](& $Command --version | Select-Object -First 1)).Trim() -ne "okto-neuron $Version") {
            throw "live-PID refusal changed the installed CLI version"
        }
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $yaml).Hash.ToLowerInvariant() -ne $configHash) {
            throw "live-PID refusal changed okto-neuron.yaml"
        }
    } finally {
        if ($guardStarted) {
            if ($guardProcess.HasExited) {
                $guardCleanupFailure = "lifecycle owner exited before the cleanup request"
            } else {
                try {
                    [IO.File]::WriteAllText($guardStopPath, "stop`n", [Text.Encoding]::ASCII)
                } catch {
                    $guardCleanupFailure = "could not request graceful lifecycle-owner cleanup: $($_.Exception.Message)"
                }
                if (-not $guardProcess.WaitForExit(5000)) {
                    $guardForced = $true
                    Stop-Process -Id $guardProcess.Id -Force -ErrorAction SilentlyContinue
                    if (-not $guardProcess.WaitForExit(5000)) {
                        $guardCleanupFailure = "lifecycle owner remained live after force fallback"
                    } elseif (-not $guardCleanupFailure) {
                        $guardCleanupFailure = "lifecycle owner required force fallback"
                    }
                }
            }
        }
        if (-not $guardStarted -or $guardProcess.HasExited) {
            if (Test-Path -LiteralPath $guardRecordPath) {
                if (-not $guardForced -and -not $guardCleanupFailure) {
                    $guardCleanupFailure = "graceful lifecycle-owner cleanup left its PID record"
                }
                Remove-Item -LiteralPath $guardRecordPath -Force -ErrorAction SilentlyContinue
            }
            Remove-Item -LiteralPath $guardHelperPath, $guardReadyPath, $guardStopPath `
                -Force -ErrorAction SilentlyContinue
            $guardProcess.Dispose()
        }
    }
    if ($guardCleanupFailure) {
        throw $guardCleanupFailure
    }
    if (Test-Path -LiteralPath $guardRecordPath) {
        throw "lifecycle owner PID record remained after cleanup: $guardRecordPath"
    }
    Assert-TokenUnchanged $tokenPath $initialToken
    if (-not (Test-Path -LiteralPath $rollbackSentinel -PathType Leaf)) {
        throw "live-PID refusal replaced the previous tool"
    }
    if ((Get-FileHash -Algorithm SHA256 `
        -LiteralPath $rollbackSentinel).Hash.ToLowerInvariant() -ne $rollbackSentinelHash) {
        throw "live-PID refusal changed the previous tool"
    }
    Write-Host "RELEASE_LIFECYCLE_LIVE_PID_REFUSAL_OK"

    Invoke-TestCli $Command @("serve", "--daemon", "--no-open")
    $status = Confirm-DefaultDaemon $Command $Version
    $rollbackBefore = [int]$status.pid
    $rollbackOutput = Join-Path $HomePath "activation-rollback.out"
    $shimRoot = Join-Path $HomePath "activation-failure-shim"
    New-Item -ItemType Directory -Force -Path $shimRoot | Out-Null
    @(
        '@echo off',
        'if "%OKTO_NEURON_TEST_FAIL_ACTIVATION%"=="1" if /I "%~1"=="tool" if /I "%~2"=="install" exit /b 77',
        '"%OKTO_NEURON_REAL_UV%" %*',
        'exit /b %ERRORLEVEL%'
    ) | Set-Content -LiteralPath (Join-Path $shimRoot "uv.cmd") -Encoding ASCII
    $oldPath = $env:Path
    $oldFailActivation = $env:OKTO_NEURON_TEST_FAIL_ACTIVATION
    $oldRealUv = $env:OKTO_NEURON_REAL_UV
    try {
        $env:OKTO_NEURON_REAL_UV = $realUv
        $env:OKTO_NEURON_TEST_FAIL_ACTIVATION = "1"
        $env:Path = "$shimRoot;$oldPath"
        $rollbackExitCode = Invoke-RawInstallerProcess $Url $rollbackOutput
    } finally {
        $env:Path = $oldPath
        $env:OKTO_NEURON_TEST_FAIL_ACTIVATION = $oldFailActivation
        $env:OKTO_NEURON_REAL_UV = $oldRealUv
        Remove-Item -LiteralPath $shimRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($rollbackExitCode -eq 0) {
        throw "forced activation failure unexpectedly succeeded"
    }
    Assert-OutputContains $rollbackOutput "candidate activation failed"
    Assert-OutputContains $rollbackOutput "restored and restarted Okto Neuron $Version"
    $status = Confirm-DefaultDaemon $Command $Version
    $rollbackAfter = [int]$status.pid
    if ($rollbackBefore -eq $rollbackAfter) {
        throw "activation rollback did not restart the previous daemon"
    }
    if (([string](& $Command --version | Select-Object -First 1)).Trim() -ne "okto-neuron $Version") {
        throw "activation rollback did not restore the CLI version"
    }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $yaml).Hash.ToLowerInvariant() -ne $configHash) {
        throw "activation rollback changed okto-neuron.yaml"
    }
    Assert-TokenUnchanged $tokenPath $initialToken
    if (-not (Test-Path -LiteralPath $rollbackSentinel -PathType Leaf)) {
        throw "activation rollback did not restore the previous-tool sentinel"
    }
    if ((Get-FileHash -Algorithm SHA256 `
        -LiteralPath $rollbackSentinel).Hash.ToLowerInvariant() -ne $rollbackSentinelHash) {
        throw "activation rollback changed the previous-tool sentinel"
    }
    if (Get-ChildItem -LiteralPath $toolRoot -Directory `
        -Filter ".okto-neuron-installer-backup-*" -ErrorAction SilentlyContinue) {
        throw "activation rollback left an installer backup directory"
    }
    Write-Host "RELEASE_LIFECYCLE_PREVIOUS_TOOL_SENTINEL_SHA256=$rollbackSentinelHash"
    Write-Host "RELEASE_LIFECYCLE_PREVIOUS_TOOL_SENTINEL_OK"
    Write-Host "RELEASE_LIFECYCLE_ACTIVATION_ROLLBACK_OK"

    Invoke-TestCli $Command @("stop", "--timeout", "30")
    Wait-TestDaemonStopped $rollbackAfter @(7777, 8201)
    Assert-TokenUnchanged $tokenPath $initialToken
    Write-Host "RELEASE_LIFECYCLE_FINAL_STOP_OK"
    Write-Host "WINDOWS_RELEASE_LIFECYCLE_OK"
}

Start-Transcript -Path $Evidence -Force
try {
    Write-Host "TEST_HOME=$TestHome"
    Write-Host "VAULT=$Vault"
    Write-Host "PROFILE=$Profile"
    if ($Profile -eq "release-lifecycle") {
        Write-Host "DRIVER_COMMIT=$DriverCommit"
        Write-Host "DRIVER_URL=$DriverUrl"
        Write-Host "DRIVER_SHA256=$DriverSha256"
        Write-Host "INSTALL_SHA256=$InstallSha256"
        Write-Host "MANIFEST_URL=$ManifestUrl"
        Write-Host "MANIFEST_SHA256=$ManifestSha256"
    }
    Write-Host "ENTRYPOINT_URL=$InstallUrl"
    Write-Host "INPUT_REDIRECTED=$([Console]::IsInputRedirected)"
    if ($Profile -in @("interactive", "release-lifecycle") -and [Console]::IsInputRedirected) {
        throw "$Profile profile requires a real Windows PowerShell terminal; redirected input is only valid for -Profile skip or -Profile custom smoke tests"
    }

    if ($Profile -eq "release-lifecycle") {
        Invoke-StoppedPredecessorUpdate $InstallUrl $ExpectedVersion $ManifestUrl $TestHome
    }
    & {
        Invoke-RestMethod -UseBasicParsing $InstallUrl | Invoke-Expression
    }

    $toolBin = (Invoke-TestNative uv @("tool", "dir", "--bin") | Select-Object -First 1)
    if ($toolBin) {
        $env:Path = "$toolBin;$env:Path"
    }
    $sandboxCli = (Get-Command okto-neuron -ErrorAction Stop).Source
    if (-not $sandboxCli) {
        throw "sandbox okto-neuron command was not found after installation"
    }
    $vaultPath = Join-Path (Join-Path (Join-Path $HOME ".okto-neuron") "vaults") $Vault
    & $sandboxCli --help | Select-Object -First 12 | Out-Host
    $cliVersion = [string](& $sandboxCli --version | Select-Object -First 1)
    if ($cliVersion.Trim() -ne "okto-neuron $ExpectedVersion") {
        throw "CLI version '$($cliVersion.Trim())' does not match $ExpectedVersion"
    }
    if ($Profile -ne "release-lifecycle") {
        $currentVault = (Invoke-TestNative $sandboxCli @("vault", "current") | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $currentVault) {
            throw "okto-neuron vault current failed after fresh installation"
        }
        if ([IO.Path]::GetFullPath($currentVault) -ne [IO.Path]::GetFullPath($vaultPath)) {
            throw "current vault '$currentVault' does not match sandbox vault '$vaultPath'"
        }
        Write-Host $currentVault
    }
    if (-not $NoServe) {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:7777/health" -TimeoutSec 5
        Write-Host "TEST_HEALTH_OK=$($health.status)"
        $version = Invoke-RestMethod -Uri "http://127.0.0.1:7777/version" -TimeoutSec 5
        if ([string]$version.okto_neuron_version -ne $ExpectedVersion) {
            throw "server version '$($version.okto_neuron_version)' does not match $ExpectedVersion"
        }
        Write-Host "TEST_VERSION_OK=$ExpectedVersion"
    }
    $yaml = Join-Path $vaultPath "okto-neuron.yaml"
    if ($Profile -eq "release-lifecycle") {
        if (Test-Path -LiteralPath $yaml) {
            throw "default app-first install unexpectedly created a vault"
        }
    } else {
        if (-not (Test-Path $yaml)) {
            throw "missing vault config: $yaml"
        }
        Write-Host "TEST_YAML=$yaml"
    }
    if ($Profile -eq "skip" -and
        (Select-String -Path $yaml -Pattern "^\s*llm:" -Quiet)) {
        throw "skip profile wrote an explicit llm block"
    }
    if ($Profile -eq "custom") {
        if (-not (Select-String -Path $yaml -Pattern "^\s*provider:\s*openai\s*$" -Quiet)) {
            throw "custom profile did not write provider: openai"
        }
        if (-not (Select-String -Path $yaml -SimpleMatch "api_base: $ApiBase" -Quiet)) {
            throw "custom profile did not write api_base: $ApiBase"
        }
        if (-not (Select-String -Path $yaml -SimpleMatch "model: $Model" -Quiet)) {
            throw "custom profile did not write model: $Model"
        }
        Write-Host "WINDOWS_CUSTOM_PROFILE_OK"
    }
    if ($Profile -eq "release-lifecycle") {
        Invoke-ReleaseLifecycle $sandboxCli $vaultPath $ExpectedVersion $InstallUrl $TestHome
    } else {
        Write-Host "WINDOWS_PUBLIC_INSTALL_TEST_OK"
    }
} finally {
    Stop-Transcript
}
'@

Set-Content -LiteralPath $Runner -Value $child -Encoding UTF8

$isolatedProductNames = @(
    "OKTO_NEURON_WHEEL",
    "OKTO_NEURON_SRC",
    "OKTO_NEURON_MANIFEST",
    "OKTO_NEURON_WHEEL_SHA256",
    "OKTO_NEURON_DEFAULT_WHEEL_URL",
    "OKTO_NEURON_DEFAULT_MANIFEST_URL",
    "OKTO_NEURON_REPO",
    "OKTO_NEURON_REF",
    "OKTO_NEURON_PACKS",
    "OKTO_NEURON_ENDPOINT",
    "OKTO_NEURON_AUTH_TOKEN",
    "OKTO_NEURON_TEST_DRIVER_COMMIT"
)
$envNames = @(
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "UV_TOOL_DIR",
    "UV_TOOL_BIN_DIR",
    "UV_INSTALL_DIR",
    "UV_UNMANAGED_INSTALL",
    "UV_PYTHON_INSTALL_DIR",
    "UV_PYTHON_BIN_DIR",
    "UV_PYTHON_CACHE_DIR",
    "UV_CACHE_DIR",
    "UV_NO_MODIFY_PATH",
    "UV_PYTHON_NO_REGISTRY",
    "UV_PYTHON_INSTALL_REGISTRY",
    "TEMP",
    "TMP",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_BIN_HOME",
    "OKTO_NEURON_NO_MCP",
    "OKTO_NEURON_NO_OPEN",
    "OKTO_NEURON_VAULT",
    "OKTO_NEURON_EXPECTED_VERSION",
    "OKTO_NEURON_NO_SERVE",
    "PATH"
) + $isolatedProductNames
$oldEnv = @{}
foreach ($name in $envNames) {
    $oldEnv[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

$childExitCode = 0
$stopError = $null
$CallerIdentity = if ($env:USERDOMAIN -and $env:USERNAME) {
    "$($env:USERDOMAIN)\$($env:USERNAME)"
} else {
    [Environment]::UserName
}
$MachineIdentity = if ($env:COMPUTERNAME) { $env:COMPUTERNAME } else { [Environment]::MachineName }
try {
    [Environment]::SetEnvironmentVariable("HOME", $TestHome, "Process")
    [Environment]::SetEnvironmentVariable("USERPROFILE", $TestHome, "Process")
    [Environment]::SetEnvironmentVariable("APPDATA", (Join-Path $TestHome "AppData\Roaming"), "Process")
    [Environment]::SetEnvironmentVariable("LOCALAPPDATA", (Join-Path $TestHome "AppData\Local"), "Process")
    [Environment]::SetEnvironmentVariable("UV_TOOL_DIR", $SandboxUvToolDir, "Process")
    [Environment]::SetEnvironmentVariable("UV_TOOL_BIN_DIR", $SandboxUvToolBin, "Process")
    [Environment]::SetEnvironmentVariable("UV_INSTALL_DIR", $SandboxUvToolBin, "Process")
    [Environment]::SetEnvironmentVariable("UV_UNMANAGED_INSTALL", $null, "Process")
    [Environment]::SetEnvironmentVariable("UV_PYTHON_INSTALL_DIR", $SandboxUvPythonDir, "Process")
    [Environment]::SetEnvironmentVariable("UV_PYTHON_BIN_DIR", (Join-Path $TestHome ".uv\python-bin"), "Process")
    [Environment]::SetEnvironmentVariable("UV_PYTHON_CACHE_DIR", (Join-Path $TestHome ".uv\python-cache"), "Process")
    [Environment]::SetEnvironmentVariable("UV_CACHE_DIR", $SandboxUvCacheDir, "Process")
    [Environment]::SetEnvironmentVariable("UV_NO_MODIFY_PATH", "1", "Process")
    [Environment]::SetEnvironmentVariable("UV_PYTHON_NO_REGISTRY", "1", "Process")
    [Environment]::SetEnvironmentVariable("UV_PYTHON_INSTALL_REGISTRY", "0", "Process")
    [Environment]::SetEnvironmentVariable("TEMP", (Join-Path $TestHome "tmp"), "Process")
    [Environment]::SetEnvironmentVariable("TMP", (Join-Path $TestHome "tmp"), "Process")
    [Environment]::SetEnvironmentVariable("XDG_DATA_HOME", (Join-Path $TestHome ".local\share"), "Process")
    [Environment]::SetEnvironmentVariable("XDG_CACHE_HOME", (Join-Path $TestHome ".cache"), "Process")
    [Environment]::SetEnvironmentVariable("XDG_CONFIG_HOME", (Join-Path $TestHome ".config"), "Process")
    [Environment]::SetEnvironmentVariable("XDG_BIN_HOME", (Join-Path $TestHome ".local\bin"), "Process")
    [Environment]::SetEnvironmentVariable("OKTO_NEURON_NO_MCP", "1", "Process")
    [Environment]::SetEnvironmentVariable("OKTO_NEURON_NO_OPEN", "1", "Process")
    if ($Profile -eq "release-lifecycle") {
        [Environment]::SetEnvironmentVariable("OKTO_NEURON_VAULT", $null, "Process")
    } else {
        [Environment]::SetEnvironmentVariable("OKTO_NEURON_VAULT", $Vault, "Process")
    }
    [Environment]::SetEnvironmentVariable("OKTO_NEURON_EXPECTED_VERSION", $ExpectedVersion, "Process")
    foreach ($name in $isolatedProductNames) {
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    if ($Profile -eq "release-lifecycle") {
        [Environment]::SetEnvironmentVariable("OKTO_NEURON_MANIFEST", $ManifestUrl, "Process")
        [Environment]::SetEnvironmentVariable("OKTO_NEURON_DEFAULT_MANIFEST_URL", $ManifestUrl, "Process")
    }
    if ($NoServe) {
        [Environment]::SetEnvironmentVariable("OKTO_NEURON_NO_SERVE", "1", "Process")
    } else {
        [Environment]::SetEnvironmentVariable("OKTO_NEURON_NO_SERVE", $null, "Process")
    }
    [Environment]::SetEnvironmentVariable(
        "PATH",
        "$SandboxUvToolBin;$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\Wbem;$env:SystemRoot\System32\WindowsPowerShell\v1.0",
        "Process"
    )

    $powershell = Join-Path $PSHOME "powershell.exe"
    $childArgs = @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        $Runner,
        "-InstallUrl",
        $InstallUrl,
        "-TestHome",
        $TestHome,
        "-Vault",
        $Vault,
        "-ExpectedVersion",
        $ExpectedVersion,
        "-Profile",
        $Profile
    )
    if ($Profile -eq "release-lifecycle") {
        $childArgs += @(
            "-DriverCommit", $DriverCommit,
            "-DriverUrl", $DriverUrl,
            "-DriverSha256", $DriverSha256,
            "-InstallSha256", $InstallSha256,
            "-ManifestUrl", $ManifestUrl,
            "-ManifestSha256", $ManifestSha256
        )
    }
    if ($ApiBase) {
        $childArgs += @("-ApiBase", $ApiBase)
    }
    if ($Model) {
        $childArgs += @("-Model", $Model)
    }
    if ($NoServe) {
        $childArgs += "-NoServe"
    }
    $childArgs += @("-Evidence", $RawEvidence)
    & $powershell @childArgs
    $childExitCode = $LASTEXITCODE
} finally {
    if ($Cleanup -or $Profile -eq "release-lifecycle") {
        try {
            Stop-TestDaemon $TestHome $Vault $SandboxUvToolBin
        } catch {
            $stopError = $_.Exception.Message
        }
    }
    foreach ($name in $envNames) {
        [Environment]::SetEnvironmentVariable($name, $oldEnv[$name], "Process")
    }
}

if (Test-Path -LiteralPath $RawEvidence -PathType Leaf) {
    Export-PublicEvidence `
        $RawEvidence $Evidence $TestHome $RealHome $CallerIdentity $MachineIdentity
}
if ($stopError) { throw $stopError }
if ($Cleanup) {
    Remove-TestSandbox $TestHome $OriginalTempRoot
} else {
    Write-Host ""
    Write-Host "Sandbox/evidence kept at:"
    Write-Host "  $TestHome"
    Write-Host "Public sanitized evidence:"
    Write-Host "  $Evidence"
    Write-Host "Private raw transcript (do not publish):"
    Write-Host "  $RawEvidence"
}
if ($childExitCode -ne 0) {
    exit $childExitCode
}
