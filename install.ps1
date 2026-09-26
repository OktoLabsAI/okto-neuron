# Okto Neuron one-shot installer for Windows PowerShell.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/OktoLabsAI/okto-neuron/main/install.ps1 | iex"
#
# Takes a fresh Windows machine from zero to a running Okto Neuron application
# wired into Claude Code: prereqs -> install tool -> serve/open the app ->
# register MCP. Vault creation and provider setup are application-first by
# default; automation may explicitly preseed one vault with OKTO_NEURON_VAULT.
#
# Upgrading from Marginalia (the pre-0.3.0 name of this product): the installer
# finds the `marginalia` uv tool, stops its daemon with that tool's own command,
# installs okto-neuron in its place, copies app-level files from ~\.marginalia
# to ~\.okto-neuron (vaults are never moved), and re-registers a user- or
# local-scope `marginalia` Claude MCP entry as `okto-neuron`, removing the old
# entry once the new one is verified as connected. Any failure before the new
# version is verified restores the exact previous `marginalia` tool.
# Every OKTO_NEURON_* variable can still be given under its pre-0.3.0
# MARGINALIA_* name (read with a warning) when the OKTO_NEURON_* one is unset.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Honour pre-0.3.0 MARGINALIA_* inputs (until 0.5): the new OKTO_NEURON_* name
# wins, the old one is read with one warning. Runs before any input is read.
foreach ($legacyVar in @(Get-ChildItem Env: | Where-Object { $_.Name -like "MARGINALIA_*" })) {
    $newVarName = "OKTO_NEURON_" + $legacyVar.Name.Substring("MARGINALIA_".Length)
    $newVarValue = [Environment]::GetEnvironmentVariable($newVarName, "Process")
    if ($null -eq $newVarValue) {
        [Environment]::SetEnvironmentVariable($newVarName, $legacyVar.Value, "Process")
        Write-Host " !! $($legacyVar.Name) is deprecated; using it as $newVarName" -ForegroundColor Yellow
    } elseif ($newVarValue -ne $legacyVar.Value) {
        Write-Host " !! both $newVarName and $($legacyVar.Name) are set; using $newVarName" -ForegroundColor Yellow
    }
}

$DefaultWheelUrl = if ($env:OKTO_NEURON_DEFAULT_WHEEL_URL) {
    $env:OKTO_NEURON_DEFAULT_WHEEL_URL
} else {
    "https://github.com/OktoLabsAI/okto-neuron/releases/download/v0.3.0/okto_neuron-0.3.0-py3-none-any.whl"
}
$DefaultManifestUrl = if ($env:OKTO_NEURON_DEFAULT_MANIFEST_URL) {
    $env:OKTO_NEURON_DEFAULT_MANIFEST_URL
} else {
    "https://raw.githubusercontent.com/OktoLabsAI/okto-neuron/main/release-manifest.json"
}
$ExpectedVersion = if ($env:OKTO_NEURON_EXPECTED_VERSION) { $env:OKTO_NEURON_EXPECTED_VERSION } else { "0.3.0" }
$Extras = "serve,litellm"
# Trace export needs mlflow in the tool's own environment; a tracking URI with no
# mlflow traces nothing, so the URI alone is enough to ask for the extra.
if ($env:OKTO_NEURON_TELEMETRY -eq "1" -or
    ($env:OKTO_NEURON_TELEMETRY -ne "0" -and $env:OKTO_NEURON_MLFLOW_TRACKING_URI)) {
    $Extras = "$Extras,telemetry"
}
$PyVersion = "3.12"
$Repo = if ($env:OKTO_NEURON_REPO) { $env:OKTO_NEURON_REPO } else { "https://github.com/OktoLabsAI/okto-neuron.git" }
$Ref = if ($env:OKTO_NEURON_REF) { $env:OKTO_NEURON_REF } else { "" }
$Vault = if ($env:OKTO_NEURON_VAULT) { $env:OKTO_NEURON_VAULT } else { "" }
$Packs = if ($env:OKTO_NEURON_PACKS) { $env:OKTO_NEURON_PACKS } else { "core,research,personal" }
$HomeRoot = Join-Path $HOME ".okto-neuron"
# Pre-0.3.0 app home. Its vaults stay where they are (document ids hash absolute
# paths), so an upgraded machine keeps creating named vaults there too.
$LegacyHomeRoot = Join-Path $HOME ".marginalia"
$VaultRoot = Join-Path $HomeRoot "vaults"
if (Test-Path -LiteralPath (Join-Path $LegacyHomeRoot "vaults")) { $VaultRoot = Join-Path $LegacyHomeRoot "vaults" }
$VaultDir = if ($Vault) { Join-Path $VaultRoot $Vault } else { "" }
$ToolName = "okto-neuron"
$CliName = "okto-neuron"
$LegacyToolName = "marginalia"
$LegacyCliName = "marginalia"
# Launchers the okto-neuron tool installs (marginalia is the warning alias).
$LauncherNames = @("okto-neuron.exe", "okto-neuron.cmd", "okto-neuron", "kg.exe", "kg.cmd", "kg", "marginalia.exe", "marginalia.cmd", "marginalia")
$RestUrl = "http://127.0.0.1:7777"
$McpUrl = "http://127.0.0.1:8201/mcp"
$DaemonTokenFile = Join-Path $HomeRoot "daemon-7777.token"
$LegacyDaemonTokenFile = Join-Path $LegacyHomeRoot "daemon-7777.token"
$DaemonRuntimeRoot = Join-Path $HomeRoot "runtime"
# `.marginalia\` inside a lifecycle root is the kept state-directory name.
$DaemonPidFile = Join-Path $DaemonRuntimeRoot ".marginalia\server.pid"
$LegacyDaemonRuntimeRoot = Join-Path $LegacyHomeRoot "runtime"
$LegacyDaemonPidFile = Join-Path $LegacyDaemonRuntimeRoot ".marginalia\server.pid"
$script:CloneTmp = $null
$script:WorkTmp = $null
$script:ToolRoot = $null
$script:ToolBin = $null
$script:BackupRoot = $null
$script:PreviousVersion = ""
$script:PreviousCommand = ""
$script:PreviousDaemonVault = ""
$script:LegacyDaemon = $false
$script:WasRunning = $false
$script:TransactionArmed = $false
$script:PreviousStopRequested = $false
$script:PreviousProcessId = 0
$script:ActivationStarted = $false
$script:ActivationCommitted = $false
$script:CandidateInstallAttempted = $false
$script:CandidateDaemonStarted = $false
$script:CandidateProcessId = 0
$script:ProductUpgrade = $false
# Product name of the install being replaced, for rollback messages.
$script:PreviousProduct = "Okto Neuron"
$script:PreviousToolName = ""
$script:PreviousCli = ""
$script:HomeMigration = $null
$script:MovedToExisted = $false

function Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Green
}

function Info([string]$Message) {
    Write-Host "    $Message"
}

function Warn([string]$Message) {
    Write-Host " !! $Message" -ForegroundColor Yellow
}

function Die([string]$Message) {
    Write-Error "error: $Message"
    exit 1
}

# Runs a native command with its stderr discarded (or, with -MergeStderr, merged
# into the output as plain text) and leaves its exit code in $LASTEXITCODE for
# the caller to check. Windows PowerShell 5.1 wraps every redirected stderr line
# of a native command in an ErrorRecord, and under this script's
# $ErrorActionPreference = "Stop" the first such line is a terminating
# NativeCommandError even when the command exits 0 (uv prints "Updated PATH ..."
# to stderr on success). Continue is scoped to this function; PowerShell 7
# never raised here, so its behaviour is unchanged. A command that cannot be
# found still throws, as before, instead of leaving a stale $LASTEXITCODE.
function Invoke-NativeCommand([string]$Command, [object[]]$Arguments = @(), [switch]$MergeStderr) {
    Get-Command -Name $Command -ErrorAction Stop | Out-Null
    $ErrorActionPreference = "Continue"
    if ($MergeStderr) {
        & $Command @Arguments 2>&1 | ForEach-Object { "$_" }
    } else {
        & $Command @Arguments 2>$null
    }
}

function Open-ApplicationUi([string]$Url) {
    try {
        Start-Process $Url -ErrorAction Stop | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Require-ExpectedWheelVersion([string]$Version) {
    if (-not $Version) {
        Die "wheel verification requires a manifest version or OKTO_NEURON_EXPECTED_VERSION"
    }
}

function Assert-ValidPreseedInputs {
    if ($Vault) { return }

    foreach ($name in @(
        "OKTO_NEURON_PACKS",
        "OKTO_NEURON_LLM_PROVIDER",
        "OKTO_NEURON_LLM_API_BASE",
        "OKTO_NEURON_LLM_MODEL",
        "OKTO_NEURON_LLM_API_KEY_ENV",
        "OKTO_NEURON_LLM_SKIP_DISCOVERY",
        "OKTO_NEURON_LLM_ALLOW_REMOTE",
        "OKTO_NEURON_ALLOW_REMOTE_LLM",
        "OKTO_NEURON_ONBOARD_NONINTERACTIVE"
    )) {
        $value = [Environment]::GetEnvironmentVariable($name, "Process")
        if ($value) {
            Die "$name requires OKTO_NEURON_VAULT; omit preseed settings and configure vaults in the Web UI, or set OKTO_NEURON_VAULT explicitly"
        }
    }
}

function Run-Checked([string]$Command, [string[]]$Arguments) {
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Command failed with exit code $LASTEXITCODE"
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

function Prompt-Value([string]$Message, [string]$Default = "") {
    if ($Default) {
        $answer = Read-Host "$Message [$Default]"
    } else {
        $answer = Read-Host $Message
    }
    if ([string]::IsNullOrWhiteSpace($answer)) {
        return $Default
    }
    return $answer
}

function Test-ProcessAlive([int]$ProcessId) {
    try {
        Get-Process -Id $ProcessId -ErrorAction Stop | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Read-ServerProcessId([string]$Path) {
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

function Find-DaemonLockRoot([int]$ProcessId) {
    if ($ProcessId -le 0) { return "" }

    # A pre-0.3.0 daemon keeps its application PID record under ~\.marginalia.
    foreach ($root in @($DaemonRuntimeRoot, $LegacyDaemonRuntimeRoot)) {
        if ((Read-ServerProcessId (Join-Path $root ".marginalia\server.pid")) -eq $ProcessId) {
            return [string]$root
        }
    }
    return ""
}

function Test-VaultHasConfig([string]$Path) {
    return ((Test-Path -LiteralPath (Join-Path $Path "okto-neuron.yaml") -PathType Leaf) -or
        (Test-Path -LiteralPath (Join-Path $Path "marginalia.yaml") -PathType Leaf))
}

function Get-PayloadVersion($Payload) {
    if (-not $Payload) { return "" }
    $names = $Payload.PSObject.Properties.Name
    if ($names -contains "okto_neuron_version" -and $Payload.okto_neuron_version) {
        return [string]$Payload.okto_neuron_version
    }
    if ($names -contains "marginalia_version") { return [string]$Payload.marginalia_version }
    return ""
}

function Get-LegacyVaultPaths {
    $paths = @()
    if ($script:PreviousCommand -and (Test-Path -LiteralPath $script:PreviousCommand)) {
        try {
            $raw = (Invoke-NativeCommand $script:PreviousCommand @("vault", "list", "--json") | Out-String)
            if ($LASTEXITCODE -eq 0 -and $raw) {
                $payload = ConvertFrom-Json $raw
                $paths += @($payload.vaults | ForEach-Object { [string]$_.path })
            }
        } catch {}
    }
    $vaultRoot = Join-Path $LegacyHomeRoot "vaults"
    if (Test-Path -LiteralPath $vaultRoot) {
        $paths += @(Get-ChildItem -LiteralPath $vaultRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "marginalia.yaml") } |
            ForEach-Object { $_.FullName })
    }
    return @($paths | Where-Object { $_ } | Sort-Object -Unique)
}

function Find-UnverifiedLiveLegacyDaemon {
    foreach ($vaultPath in @(Get-LegacyVaultPaths)) {
        $recordPath = Join-Path $vaultPath ".marginalia\server.pid"
        $recordProcessId = Read-ServerProcessId $recordPath
        if ($recordProcessId -gt 0 -and (Test-ProcessAlive $recordProcessId)) {
            return [pscustomobject]@{
                pid = $recordProcessId
                vault = $vaultPath
            }
        }
    }
    return $null
}

function Find-VerifiedLegacyLockRoot([int]$ProcessId, [string]$VaultPath) {
    if ($ProcessId -le 0 -or -not $VaultPath) { return "" }
    if (-not (Test-Path -LiteralPath (Join-Path $VaultPath "marginalia.yaml") -PathType Leaf)) {
        return ""
    }
    $recordPath = Join-Path $VaultPath ".marginalia\server.pid"
    if ((Read-ServerProcessId $recordPath) -eq $ProcessId) {
        return $VaultPath
    }
    return ""
}

function Test-ClaudeMcpRegistrationMatches([string]$Output, [string]$ExpectedUrl, [string]$ScopeLabel = "User") {
    if (-not $Output -or -not $ExpectedUrl) { return $false }

    $fieldPattern = '(?m)^[ \t]*{0}:[ \t]*([^\r\n]*?)[ \t]*\r?$'
    $scopeMatches = [regex]::Matches($Output, ($fieldPattern -f "Scope"))
    $statusMatches = [regex]::Matches($Output, ($fieldPattern -f "Status"))
    $typeMatches = [regex]::Matches($Output, ($fieldPattern -f "Type"))
    $urlMatches = [regex]::Matches($Output, ($fieldPattern -f "URL"))
    if ($scopeMatches.Count -ne 1 -or $statusMatches.Count -ne 1 -or
        $typeMatches.Count -ne 1 -or $urlMatches.Count -ne 1) {
        return $false
    }

    $scope = $scopeMatches[0].Groups[1].Value
    $status = $statusMatches[0].Groups[1].Value
    $type = $typeMatches[0].Groups[1].Value
    $url = $urlMatches[0].Groups[1].Value
    $hasUserScope = $scope -cmatch ('^' + $ScopeLabel + ' config(?:[ \t]+\([^()]*\))?$')
    # Claude prints a glyph before "Connected" (U+2713). When the console
    # output encoding is not UTF-8 (Windows PowerShell 5.1 defaults to the OEM
    # code page) its three UTF-8 bytes decode as three non-ASCII characters,
    # some of them letters (e.g. cp437 "Γ£ô"). Accept one symbol or a run of
    # non-ASCII characters as the glyph; "Not Connected" still fails.
    $isConnected = $status -cmatch '^(?:[^\p{L}\p{N}\s]|[^\x00-\x7F]+)?\s*Connected$'
    return ($hasUserScope -and $isConnected -and $type -ceq "http" -and
        $url -ceq $ExpectedUrl)
}

# The same entry checked without a running daemon, which Claude can only report
# as "Failed to connect". Everything the installer wrote is verified instead:
# one entry, the scope, http type, the URL, and one Authorization header
# carrying exactly this daemon's credential.
function Test-ClaudeMcpRegistrationWritten([string]$Output, [string]$ExpectedUrl, [string]$ScopeLabel, [string]$Token) {
    if (-not $Output -or -not $ExpectedUrl -or -not $Token) { return $false }

    $fieldPattern = '(?m)^[ \t]*{0}:[ \t]*([^\r\n]*?)[ \t]*\r?$'
    $scopeMatches = [regex]::Matches($Output, ($fieldPattern -f "Scope"))
    $typeMatches = [regex]::Matches($Output, ($fieldPattern -f "Type"))
    $urlMatches = [regex]::Matches($Output, ($fieldPattern -f "URL"))
    $authMatches = [regex]::Matches($Output, ($fieldPattern -f "Authorization"))
    if ($scopeMatches.Count -ne 1 -or $typeMatches.Count -ne 1 -or
        $urlMatches.Count -ne 1 -or $authMatches.Count -ne 1) {
        return $false
    }

    $hasScope = $scopeMatches[0].Groups[1].Value -cmatch ('^' + $ScopeLabel + ' config(?:[ \t]+\([^()]*\))?$')
    return ($hasScope -and $typeMatches[0].Groups[1].Value -ceq "http" -and
        $urlMatches[0].Groups[1].Value -ceq $ExpectedUrl -and
        $authMatches[0].Groups[1].Value -ceq "Bearer $Token")
}

function Get-ClaudeMcpRegistrationScope([string]$Output) {
    if ($Output -cmatch '(?m)^\s*Scope:\s*Local config(?:\s+\([^()]*\))?\s*$') {
        return "local"
    }
    if ($Output -cmatch '(?m)^\s*Scope:\s*Project config(?:\s+\([^()]*\))?\s*$') {
        return "project"
    }
    if ($Output -cmatch '(?m)^\s*Scope:\s*User config(?:\s+\([^()]*\))?\s*$') {
        return "user"
    }
    return "unknown"
}

function Assert-NoUndiscoveredLiveDaemon {
    foreach ($root in @($DaemonRuntimeRoot, $LegacyDaemonRuntimeRoot)) {
        $recordPath = Join-Path $root ".marginalia\server.pid"
        if (-not (Test-Path -LiteralPath $recordPath)) { continue }
        $recordProcessId = Read-ServerProcessId $recordPath
        if ($recordProcessId -gt 0 -and (Test-ProcessAlive $recordProcessId)) {
            $stopCli = if ($script:PreviousCli) { $script:PreviousCli } else { $CliName }
            Die "a live Okto Neuron process (pid $recordProcessId) was found at '$root', but status at $RestUrl was unavailable. Run: $stopCli stop. Custom endpoint/ports may require manual stop. Update aborted before activation."
        }
    }
    if ($script:PreviousVersion -eq "0.0.40") {
        $legacy = Find-UnverifiedLiveLegacyDaemon
        if ($legacy) {
            Die "a live Marginalia 0.0.40 process (pid $($legacy.pid)) has a vault-scoped PID record at '$($legacy.vault)\.marginalia\server.pid', but status at $RestUrl was unavailable. Run: marginalia stop --vault `"$($legacy.vault)`". Custom endpoint/ports may require manual stop. Update aborted before activation."
        }
    }
}

function Test-TcpPort([int]$Port) {
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

function Get-CliCommand([string]$Name = $CliName) {
    foreach ($candidate in @($Name, "$Name.exe", "$Name.cmd")) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) {
            return $cmd.Source
        }
    }
    return $null
}

function Get-DaemonStatus {
    $command = $script:PreviousCommand
    if (-not $command) { $command = Get-CliCommand }
    if ($command) {
        try {
            $raw = (Invoke-NativeCommand $command @("status", "--json", "--timeout", "2") | Out-String)
            if ($LASTEXITCODE -eq 0 -and $raw) {
                $status = ConvertFrom-Json $raw
                if ($status.pid) { return $status }
            }
        } catch {}
    }

    try {
        $status = Invoke-RestMethod -Uri "$RestUrl/api/v1/status" -TimeoutSec 2 -ErrorAction Stop
        if ($status.pid) { return $status }
    } catch {
        try {
            $status = Invoke-RestMethod -Uri "$RestUrl/health" -TimeoutSec 2 -ErrorAction Stop
            if ($status.pid) { return $status }
        } catch {}
    }
    return $null
}

function Get-ToolVersion([string]$Root, [string]$Tool = $ToolName) {
    $python = Join-Path (Join-Path $Root $Tool) "Scripts\python.exe"
    if (-not (Test-Path $python)) { return "" }
    try {
        return ([string](& $python -c 'import importlib.metadata, sys; print(importlib.metadata.version(sys.argv[1]))' $Tool)).Trim()
    } catch {
        return ""
    }
}

# Which installed uv tool this run replaces.
function Find-PreviousTool {
    $ownPython = Join-Path (Join-Path $script:ToolRoot $ToolName) "Scripts\python.exe"
    $legacyPython = Join-Path (Join-Path $script:ToolRoot $LegacyToolName) "Scripts\python.exe"
    if (Test-Path -LiteralPath $ownPython) {
        # Joao's 2026-09 Okto Neuron MVP was also a uv tool named okto-neuron
        # (command `neuron`). Never replace or modify it.
        Invoke-NativeCommand $ownPython @("-c", "import okto_neuron._compat") | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Die "a different 'okto-neuron' uv tool is installed (the Okto Neuron MVP, command 'neuron'). This installer will not replace it. Keep it by stopping here, or remove it yourself with 'uv tool uninstall okto-neuron' and re-run."
        }
        if (Test-Path -LiteralPath $legacyPython) {
            Die "both the okto-neuron and the pre-0.3.0 marginalia uv tools are installed. Remove the old one with 'uv tool uninstall marginalia' and re-run."
        }
        $script:PreviousToolName = $ToolName
        $script:PreviousCli = $CliName
    } elseif (Test-Path -LiteralPath $legacyPython) {
        $script:ProductUpgrade = $true
        $script:PreviousProduct = "Marginalia"
        $script:PreviousToolName = $LegacyToolName
        $script:PreviousCli = $LegacyCliName
    }
    if ($script:PreviousToolName) {
        $script:PreviousVersion = Get-ToolVersion $script:ToolRoot $script:PreviousToolName
        $script:PreviousCommand = [string](Get-CliCommand $script:PreviousCli)
    }
    if ($script:ProductUpgrade) {
        Info "found Marginalia $($script:PreviousVersion) (the pre-0.3.0 name of Okto Neuron); it will be replaced"
    }
    if (Test-Path -LiteralPath (Join-Path $HomeRoot "grafx")) {
        Info "found Okto Neuron MVP memory at $(Join-Path $HomeRoot 'grafx'); it is left untouched"
    }
}

# Copy app-level files from ~\.marginalia into ~\.okto-neuron (vaults are never
# moved) and remember what this run created, so a rollback can remove it again.
function Invoke-AppHomeMigration([string]$Cli) {
    if (-not (Test-Path -LiteralPath $LegacyHomeRoot)) { return }
    $script:MovedToExisted = Test-Path -LiteralPath (Join-Path $LegacyHomeRoot "MOVED_TO")
    $raw = (Invoke-NativeCommand $Cli @("migrate-home", "--json") | Out-String)
    if ($LASTEXITCODE -ne 0 -or -not $raw) {
        Die "could not copy app-level files from $LegacyHomeRoot to $HomeRoot"
    }
    $script:HomeMigration = ConvertFrom-Json $raw
    $copied = @($script:HomeMigration.copied)
    $copiedText = if ($copied.Count -gt 0) { $copied -join ", " } else { "none" }
    Info "copied app files from $LegacyHomeRoot to ${HomeRoot}: $copiedText"
    if (Test-Path -LiteralPath (Join-Path $LegacyHomeRoot "vaults")) {
        Info "vaults stay in $(Join-Path $LegacyHomeRoot 'vaults') (document ids depend on their paths)"
    }
}

function Undo-AppHomeMigration {
    if (-not $script:HomeMigration) { return }
    foreach ($name in @($script:HomeMigration.copied)) {
        $name = [string]$name
        if (-not $name -or $name -match '[\\/]' -or $name -in @(".", "..")) { continue }
        $path = Join-Path $HomeRoot $name
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force -ErrorAction Stop }
    }
    if (-not $script:MovedToExisted) {
        $pointer = Join-Path $LegacyHomeRoot "MOVED_TO"
        if (Test-Path -LiteralPath $pointer) { Remove-Item -LiteralPath $pointer -Force -ErrorAction Stop }
    }
    $script:HomeMigration = $null
}

function Get-ServerVersion([string]$Command, [string]$VaultPath = "") {
    try {
        $statusArgs = @("status", "--json", "--timeout", "2")
        if ($VaultPath) { $statusArgs += @("--vault", $VaultPath) }
        $payload = (Invoke-NativeCommand $Command $statusArgs | Out-String)
        if ($LASTEXITCODE -eq 0 -and $payload) {
            return (Get-PayloadVersion (ConvertFrom-Json $payload))
        }
    } catch {}
    try {
        return (Get-PayloadVersion (Invoke-RestMethod -Uri "$RestUrl/version" -TimeoutSec 2 -ErrorAction Stop))
    } catch {
        return ""
    }
}

function Wait-ProcessExit([int]$ProcessId, [int]$TimeoutSeconds) {
    if ($ProcessId -le 0) { return $true }
    for ($i = 0; $i -lt $TimeoutSeconds; $i++) {
        if (-not (Test-ProcessAlive $ProcessId)) { return $true }
        Start-Sleep -Seconds 1
    }
    return (-not (Test-ProcessAlive $ProcessId))
}

function Stop-CandidateDaemonForRollback {
    if (-not $script:CandidateDaemonStarted) { return }

    $recordPath = $DaemonPidFile
    $candidateProcessId = $script:CandidateProcessId
    if ($candidateProcessId -le 0) {
        $candidateProcessId = Read-ServerProcessId $recordPath
        if ($candidateProcessId -gt 0) { $script:CandidateProcessId = $candidateProcessId }
    }
    $candidate = Join-Path $script:ToolBin "$CliName.exe"
    if (Test-Path -LiteralPath $candidate) {
        Invoke-NativeCommand $candidate @("stop", "--timeout", "10") | Out-Null
    }

    for ($i = 0; $i -lt 15; $i++) {
        if ($candidateProcessId -le 0) {
            $candidateProcessId = Read-ServerProcessId $recordPath
            if ($candidateProcessId -gt 0) { $script:CandidateProcessId = $candidateProcessId }
        }
        $processAlive = $candidateProcessId -gt 0 -and (Test-ProcessAlive $candidateProcessId)
        if (-not $processAlive -and -not (Test-TcpPort 7777) -and -not (Test-TcpPort 8201)) {
            # When serve itself failed there may be no PID to observe. Require a
            # short quiet window so a just-spawned detached child cannot race the
            # environment deletion.
            if ($candidateProcessId -gt 0 -or $i -ge 2) {
                $script:CandidateDaemonStarted = $false
                return
            }
        }
        Start-Sleep -Seconds 1
    }

    if ($candidateProcessId -gt 0 -and (Test-ProcessAlive $candidateProcessId)) {
        throw "candidate daemon pid $candidateProcessId is still live after graceful shutdown; active environment and backup retained"
    }
    if ((Test-TcpPort 7777) -or (Test-TcpPort 8201)) {
        throw "candidate daemon ports remain open after shutdown; active environment and backup retained"
    }
    $script:CandidateDaemonStarted = $false
}

function Restore-PreviousDaemonState {
    if (-not $script:WasRunning) { return }

    # Before activation, a failed/refused stop may leave the original process
    # untouched. Do not launch a duplicate daemon or describe that state as an
    # incomplete rollback; the prior running state is already preserved.
    if (-not $script:ActivationStarted -and $script:PreviousProcessId -gt 0 -and
        (Test-ProcessAlive $script:PreviousProcessId)) {
        Info "previous $($script:PreviousProduct) daemon remains running (pid $($script:PreviousProcessId))"
        $script:PreviousStopRequested = $false
        return
    }

    if ($script:PreviousStopRequested -and $script:PreviousProcessId -gt 0 -and
        (Test-ProcessAlive $script:PreviousProcessId)) {
        if (-not (Wait-ProcessExit $script:PreviousProcessId 35)) {
            throw "previous daemon pid $($script:PreviousProcessId) is still draining; restart could not be verified"
        }
    } elseif (-not $script:PreviousStopRequested) {
        return
    }

    # After a rollback the restored launcher is the previous tool's own command
    # (marginalia when the previous install predates the rename).
    $restored = Get-CliCommand $(if ($script:PreviousCli) { $script:PreviousCli } else { $CliName })
    if (-not $restored) { $restored = $script:PreviousCommand }
    if (-not $restored -or -not (Test-Path -LiteralPath $restored)) {
        throw "previous daemon command could not be restored"
    }
    $restartArgs = @("serve", "--daemon")
    $serveHelp = (Invoke-NativeCommand $restored @("serve", "--help") | Out-String)
    if ($serveHelp -match '(?m)--no-open\b') {
        $restartArgs += "--no-open"
    }
    if ($script:PreviousDaemonVault) {
        $restartArgs += @("--vault", $script:PreviousDaemonVault)
    }
    Invoke-NativeCommand $restored $restartArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "previous daemon could not be restarted" }
    $runningVersion = ""
    for ($i = 0; $i -lt 30; $i++) {
        $runningVersion = Get-ServerVersion $restored $script:PreviousDaemonVault
        if ($runningVersion) { break }
        Start-Sleep -Seconds 1
    }
    if (-not $runningVersion -or
        ($script:PreviousVersion -and $runningVersion -ne $script:PreviousVersion)) {
        throw "previous daemon restart could not be verified"
    }
    Info "restored and restarted $($script:PreviousProduct) $($script:PreviousVersion)"
}

function Restore-PreviousTool {
    if (-not $script:TransactionArmed) { return }
    Warn "installation failed; restoring the previous installation"

    if ($script:ActivationStarted) {
        Stop-CandidateDaemonForRollback
        Undo-AppHomeMigration
        $previousTool = if ($script:PreviousToolName) { $script:PreviousToolName } else { $ToolName }
        $activeTool = Join-Path $script:ToolRoot $ToolName
        $restoreTool = Join-Path $script:ToolRoot $previousTool
        $backupTool = if ($script:BackupRoot) { Join-Path $script:BackupRoot "tool" } else { "" }
        $hasToolBackup = $backupTool -and (Test-Path -LiteralPath $backupTool)
        $activeVersion = Get-ToolVersion $script:ToolRoot $previousTool
        $replaceActiveInstallation = (
            $hasToolBackup -or $script:CandidateInstallAttempted -or -not $script:PreviousVersion
        )
        if ($replaceActiveInstallation) {
            if (Test-Path -LiteralPath $activeTool) {
                Remove-DirectoryTree $activeTool
            }
        } elseif ($activeVersion -ne $script:PreviousVersion) {
            throw "previous tool backup is missing and the active version cannot be verified; files retained"
        }
        if ($hasToolBackup) {
            Move-Item -LiteralPath $backupTool -Destination $restoreTool -Force -ErrorAction Stop
        }
        $backupBin = if ($script:BackupRoot) { Join-Path $script:BackupRoot "bin" } else { "" }
        foreach ($name in $LauncherNames) {
            $launcher = Join-Path $script:ToolBin $name
            $backupLauncher = if ($backupBin) { Join-Path $backupBin $name } else { "" }
            $hasLauncherBackup = $backupLauncher -and (Test-Path -LiteralPath $backupLauncher)
            if (($hasLauncherBackup -or $script:CandidateInstallAttempted -or -not $script:PreviousVersion) -and
                (Test-Path -LiteralPath $launcher)) {
                Remove-Item -LiteralPath $launcher -Force -ErrorAction Stop
            }
            if ($hasLauncherBackup) {
                Move-Item -LiteralPath $backupLauncher -Destination $launcher -Force -ErrorAction Stop
            }
        }

        $restoredVersion = Get-ToolVersion $script:ToolRoot $previousTool
        if ($script:PreviousVersion -and $restoredVersion -ne $script:PreviousVersion) {
            throw "previous tool restoration could not be verified (expected $($script:PreviousVersion), got $restoredVersion)"
        }
    }

    Restore-PreviousDaemonState
    if (-not $script:WasRunning) {
        Info "restored $($script:PreviousProduct) $($script:PreviousVersion); daemon remains stopped"
    }
    $script:ActivationStarted = $false
    $script:CandidateInstallAttempted = $false
    $script:TransactionArmed = $false
    $script:PreviousStopRequested = $false
}

function Remove-InstallerTemps([switch]$KeepBackup) {
    if ($script:BackupRoot -and -not $KeepBackup) {
        Remove-DirectoryTree $script:BackupRoot
    } elseif ($script:BackupRoot -and $KeepBackup) {
        Warn "rollback incomplete; previous tool backup retained at $($script:BackupRoot)"
    }
    if ($script:WorkTmp) { Remove-DirectoryTree $script:WorkTmp }
    if ($script:CloneTmp) { Remove-DirectoryTree $script:CloneTmp }
}

# Rollback half of Remove-LegacyMcp: put the pre-0.3.0 `marginalia` entry back
# exactly as it was (same scope, endpoint and daemon credential).
$script:LegacyMcpRemovedScope = ""
function Restore-LegacyMcp {
    if (-not $script:LegacyMcpRemovedScope) { return }
    $scope = $script:LegacyMcpRemovedScope
    $script:LegacyMcpRemovedScope = ""
    Invoke-NativeCommand claude @("mcp", "add", "--scope", $scope, "--transport", "http", $LegacyCliName, $script:GlobalMcpUrl, "--header", "Authorization: Bearer $($script:McpAuthToken)") | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Info "re-added the old '$LegacyCliName' $scope-scope Claude MCP entry"
    } else {
        Warn "could not re-add the old '$LegacyCliName' $scope-scope Claude MCP entry; add it back with: claude mcp add --scope $scope --transport http $LegacyCliName $($script:GlobalMcpUrl) --header `"Authorization: Bearer <daemon token>`""
    }
}

trap {
    $originalError = $_
    $keepBackup = $false
    $restoreFailure = $null
    try { Restore-LegacyMcp } catch { Warn "could not restore the old MCP entry: $($_.Exception.Message)" }
    try {
        if ($script:TransactionArmed -and -not $script:ActivationCommitted) {
            $keepBackup = $true
            Restore-PreviousTool
            $keepBackup = $false
        }
    } catch {
        $restoreFailure = $_
        $keepBackup = $true
    } finally {
        Remove-InstallerTemps -KeepBackup:$keepBackup
    }
    if ($restoreFailure) {
        Warn "rollback incomplete: $($restoreFailure.Exception.Message)"
    }
    throw $originalError
}

# The trap above covers the whole script, including statements that come before
# it, and every function it reaches must already be defined when it fires. So
# nothing that can fail runs before this point: the first check comes after the
# last function the trap needs.
Assert-ValidPreseedInputs

Write-Host "Okto Neuron installer - local-first knowledge graph for Claude Code"

Step "Checking uv (the package manager Okto Neuron runs through)"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Info "uv not found - installing from astral.sh ..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $localBin = Join-Path $HOME ".local\bin"
    $cargoBin = Join-Path $HOME ".cargo\bin"
    $env:Path = "$localBin;$cargoBin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Die "uv installed but is not on PATH; restart PowerShell and re-run."
    }
}
Info "uv: $((Get-Command uv).Source)"

# A prior uv tool can exist even when this shell has not loaded uv's PATH
# update yet. Make it reachable before update detection needs to stop it.
$preinstallToolBin = (Invoke-NativeCommand uv @("tool", "dir", "--bin") | Select-Object -First 1)
if ($preinstallToolBin) {
    $env:Path = "$preinstallToolBin;$env:Path"
}
$script:ToolRoot = (Invoke-NativeCommand uv @("tool", "dir") | Select-Object -First 1)
$script:ToolBin = if ($preinstallToolBin) { $preinstallToolBin } else { Join-Path $HOME ".local\bin" }
$script:WorkTmp = Join-Path ([IO.Path]::GetTempPath()) ("okto-neuron-install-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $script:WorkTmp | Out-Null

Step "Ensuring Python $PyVersion (uv-managed; no system Python touched)"
& uv python install $PyVersion | Out-Null

Step "Resolving and staging the Okto Neuron candidate"
$spec = ""
$candidateKind = ""
$wheelSource = ""
$sourcePath = ""
$wheel = if ($env:OKTO_NEURON_WHEEL) { $env:OKTO_NEURON_WHEEL } else { "" }
$src = if ($env:OKTO_NEURON_SRC) { $env:OKTO_NEURON_SRC } else { "" }
if ($wheel) {
    Info "using wheel: $wheel"
    $candidateKind = "wheel"
    $wheelSource = $wheel
} elseif ($src) {
    if (-not (Test-Path (Join-Path $src "pyproject.toml"))) {
        Die "OKTO_NEURON_SRC has no pyproject.toml: $src"
    }
    Info "using checkout: $src"
    $candidateKind = "source"
    $sourcePath = $src
} elseif ($DefaultWheelUrl) {
    Info "using release wheel: $DefaultWheelUrl"
    $candidateKind = "wheel"
    $wheelSource = $DefaultWheelUrl
} else {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Die "git not found; install git or set OKTO_NEURON_SRC / OKTO_NEURON_WHEEL."
    }
    $script:CloneTmp = Join-Path $script:WorkTmp "source"
    New-Item -ItemType Directory -Path $script:CloneTmp | Out-Null
    Info "cloning $Repo ..."
    $cloneArgs = @("clone", "--depth", "1")
    if ($Ref) {
        $cloneArgs += @("--branch", $Ref)
    }
    $cloneArgs += @($Repo, $script:CloneTmp)
    & git @cloneArgs
    if ($LASTEXITCODE -ne 0) {
        Die "clone failed. Check OKTO_NEURON_REPO and your network, or pass OKTO_NEURON_SRC=<path> / OKTO_NEURON_WHEEL=<url>."
    }
    $candidateKind = "source"
    $sourcePath = $script:CloneTmp
}

if ($candidateKind -eq "wheel") {
    $manifestSource = if ($env:OKTO_NEURON_MANIFEST) { $env:OKTO_NEURON_MANIFEST } elseif ($wheelSource -eq $DefaultWheelUrl) { $DefaultManifestUrl } else { "" }
    $manifest = $null
    if ($manifestSource) {
        try {
            if ($manifestSource -match '^https?://') {
                $manifest = Invoke-RestMethod -Uri $manifestSource -TimeoutSec 30 -ErrorAction Stop
            } else {
                $manifestPath = $manifestSource -replace '^file://', ''
                $manifest = Get-Content -Raw $manifestPath | ConvertFrom-Json
            }
        } catch {
            Die "could not load release manifest: $manifestSource"
        }
        foreach ($field in @("version", "wheel_url", "wheel", "sha256")) {
            if (-not $manifest.$field) { Die "release manifest is missing $field" }
        }
        $manifestWheel = [string]$manifest.wheel
        if ((Split-Path -Leaf $manifestWheel) -ne $manifestWheel) {
            Die "release manifest wheel must be a filename, not a path"
        }
        if ($ExpectedVersion -and [string]$manifest.version -ne $ExpectedVersion) {
            Die "release manifest version $($manifest.version) does not match expected $ExpectedVersion"
        }
        $ExpectedVersion = [string]$manifest.version
        if ($wheelSource -match '^https?://') {
            if ($wheelSource -ne [string]$manifest.wheel_url) { Die "wheel URL does not match release manifest" }
        } elseif ((Split-Path -Leaf $wheelSource) -ne [string]$manifest.wheel) {
            Die "wheel filename does not match release manifest"
        }
    }

    Require-ExpectedWheelVersion $ExpectedVersion

    $expectedSha = if ($env:OKTO_NEURON_WHEEL_SHA256) { $env:OKTO_NEURON_WHEEL_SHA256 } elseif ($manifest) { [string]$manifest.sha256 } else { "" }
    if (-not $expectedSha) { Die "wheel verification requires OKTO_NEURON_MANIFEST or OKTO_NEURON_WHEEL_SHA256" }
    if ($manifest -and $env:OKTO_NEURON_WHEEL_SHA256 -and $env:OKTO_NEURON_WHEEL_SHA256 -ne [string]$manifest.sha256) {
        Die "OKTO_NEURON_WHEEL_SHA256 does not match the release manifest"
    }
    if ($expectedSha -notmatch '^[0-9a-fA-F]{64}$') { Die "wheel SHA-256 must be 64 hexadecimal characters" }

    $wheelName = if ($manifest) { [string]$manifest.wheel } else {
        if ($wheelSource -match '^https?://') {
            $wheelUriPath = ([Uri]$wheelSource).AbsolutePath
            Split-Path -Leaf $wheelUriPath
        } else {
            Split-Path -Leaf $wheelSource
        }
    }
    if (-not $wheelName.EndsWith(".whl", [StringComparison]::OrdinalIgnoreCase)) { Die "wheel filename must end in .whl" }
    $candidateWheel = Join-Path $script:WorkTmp $wheelName
    if ($wheelSource -match '^https?://') {
        Invoke-WebRequest -UseBasicParsing -Uri $wheelSource -OutFile $candidateWheel
    } else {
        Copy-Item -Force ($wheelSource -replace '^file://', '') $candidateWheel
    }
    $actualSha = (Get-FileHash -Algorithm SHA256 $candidateWheel).Hash.ToLowerInvariant()
    if ($actualSha -ne $expectedSha.ToLowerInvariant()) {
        Die "wheel SHA-256 mismatch; expected $expectedSha, got $actualSha"
    }
    Info "verified wheel SHA-256: $actualSha"
    $spec = "${candidateWheel}[$Extras]"
} else {
    $spec = "${sourcePath}[$Extras]"
}

$stageVenv = Join-Path $script:WorkTmp "stage"
Run-Checked "uv" @("venv", "--python", $PyVersion, $stageVenv)
$stagePython = Join-Path $stageVenv "Scripts\python.exe"
$stageCli = Join-Path $stageVenv "Scripts\okto-neuron.exe"
Run-Checked "uv" @("pip", "install", "--python", $stagePython, $spec)
$candidateVersion = ([string](& $stagePython -c 'import importlib.metadata; print(importlib.metadata.version(''okto-neuron''))')).Trim()
if ($ExpectedVersion -and $candidateVersion -ne $ExpectedVersion) {
    Die "staged Okto Neuron $candidateVersion, expected $ExpectedVersion"
}
$ExpectedVersion = $candidateVersion
if (-not (Test-Path $stageCli)) { Die "staged wheel did not install the okto-neuron command" }
Invoke-NativeCommand $stageCli @("--help") | Out-Null
if ($LASTEXITCODE -ne 0) { Die "staged okto-neuron command failed its help smoke" }
$stagedCliVersion = [string](Invoke-NativeCommand $stageCli @("--version") | Select-Object -First 1)
if ($LASTEXITCODE -eq 0 -and $stagedCliVersion.Trim() -ne "okto-neuron $candidateVersion") {
    Die "staged CLI does not match package version $candidateVersion"
}
Info "staged Okto Neuron $candidateVersion; active installation is still untouched"

$upgrade = $false
$wasRunning = $false
$oldPid = 0
$oldLockRoot = ""
Find-PreviousTool
$status = Get-DaemonStatus
if (-not $status) {
    # A daemon on a custom endpoint/port is invisible to the default REST probe.
    # Fail closed on any live application PID record before moving the installed tool.
    Assert-NoUndiscoveredLiveDaemon
}
if ($status) {
    $upgrade = $true
    $wasRunning = $true
    $script:WasRunning = $true
    if ($status.PSObject.Properties.Name -contains "pid") {
        $oldPid = [int]$status.pid
    }
    $oldLockRoot = Find-DaemonLockRoot $oldPid
    if (-not $oldLockRoot) {
        $statusVersion = Get-PayloadVersion $status
        $statusVault = if ($status.PSObject.Properties.Name -contains "vault_path") {
            [string]$status.vault_path
        } else { "" }
        if ($script:PreviousVersion -ne "0.0.40" -or $statusVersion -ne "0.0.40") {
            Die "Okto Neuron status reported pid $oldPid, but its application lifecycle lock could not be verified; update aborted before shutdown"
        }
        $oldLockRoot = Find-VerifiedLegacyLockRoot $oldPid $statusVault
        if (-not $oldLockRoot) {
            $reportedVault = if ($statusVault) { $statusVault } else { "unknown" }
            Die "Marginalia 0.0.40 status reported pid $oldPid and vault $reportedVault, but the matching vault-scoped lifecycle lock could not be verified; update aborted before shutdown"
        }
        $script:LegacyDaemon = $true
        $script:PreviousDaemonVault = $statusVault
    }
    if ($status.PSObject.Properties.Name -contains "endpoint" -and $status.endpoint) {
        $statusEndpoint = ([string]$status.endpoint).TrimEnd('/')
        if ($statusEndpoint -notin @($RestUrl, "http://localhost:7777")) {
            if ($script:LegacyDaemon) {
                Die "live Marginalia 0.0.40 daemon uses custom endpoint $statusEndpoint; update aborted before shutdown because the installer cannot preserve custom ports automatically. Stop it first: marginalia stop --vault `"$($script:PreviousDaemonVault)`""
            }
            Die "live Okto Neuron daemon uses custom endpoint $statusEndpoint; update aborted before shutdown because the installer cannot preserve custom ports automatically. Stop it first: $(if ($script:PreviousCli) { $script:PreviousCli } else { $CliName }) stop"
        }
    }
    Step "Existing $($script:PreviousProduct) daemon detected - updating in place"
    $existing = if ($script:PreviousCommand) { $script:PreviousCommand } else { Get-CliCommand }
    if (-not $existing) { Die "daemon is running but its installed command was not found" }
    $script:PreviousProcessId = $oldPid
    # Arm rollback before requesting shutdown. Any error or interruption from
    # this point must leave the existing tool in place and restore its daemon.
    $script:TransactionArmed = $true
    $script:PreviousStopRequested = $true
    $stopArgs = @("stop", "--timeout", "30")
    if ($script:LegacyDaemon) {
        $stopArgs = @("stop", "--vault", $script:PreviousDaemonVault, "--timeout", "30")
    }
    if ($script:ProductUpgrade -and -not $script:LegacyDaemon) {
        # The pre-0.3.0 daemon's lifecycle record lives under ~\.marginalia,
        # which only that version's own command owns.
        & $existing @stopArgs
    } else {
        & $stageCli @stopArgs
    }
    if ($LASTEXITCODE -ne 0) {
        Die "could not stop the verified daemon (pid $oldPid); update aborted before replacing the installed tool"
    }
    for ($i = 0; $i -lt 10; $i++) {
        $processAlive = $oldPid -gt 0 -and (Test-ProcessAlive $oldPid)
        if (-not $processAlive -and -not (Test-TcpPort 7777)) {
            break
        }
        Start-Sleep -Seconds 1
    }
    if (($oldPid -gt 0 -and (Test-ProcessAlive $oldPid)) -or (Test-TcpPort 7777)) {
        Die "old daemon$(if ($oldPid -gt 0) { " (pid $oldPid)" }) still owns its process or port after a successful stop; update aborted before replacing the installed tool"
    }
    Info "stopped the running daemon - it will restart on the new version below"
} elseif (Test-TcpPort 7777) {
    Die "port 7777 is in use but Okto Neuron status is unavailable; update aborted"
} elseif ($script:PreviousToolName -or
          (@($HomeRoot, $LegacyHomeRoot) | Where-Object { Test-Path (Join-Path $_ "vaults") } | ForEach-Object {
              Get-ChildItem -Path (Join-Path $_ "vaults") -Include "okto-neuron.yaml", "marginalia.yaml" -Recurse -Depth 2 -ErrorAction SilentlyContinue
          } | Select-Object -First 1)) {
    # Daemon isn't up (crashed, machine rebooted, whatever) but this machine
    # was already set up before - a re-run should update in place, not treat
    # this as a fresh install and re-run vault-create/onboard against existing
    # state (which dies noninteractively).
    $upgrade = $true
    Step "Existing Okto Neuron install detected (daemon not running) - updating in place"
}

Step "Activating staged Okto Neuron $candidateVersion"
$script:TransactionArmed = $true
$script:BackupRoot = Join-Path $script:ToolRoot (".okto-neuron-installer-backup-" + [guid]::NewGuid())
$backupBin = Join-Path $script:BackupRoot "bin"
New-Item -ItemType Directory -Path $backupBin -Force | Out-Null
$script:ActivationStarted = $true
# A pre-0.3.0 `marginalia` tool is moved aside the same way, so a failure
# restores exactly that tool under its own name.
if ($script:PreviousToolName) {
    $activeTool = Join-Path $script:ToolRoot $script:PreviousToolName
    if (Test-Path $activeTool) { Move-Item $activeTool (Join-Path $script:BackupRoot "tool") }
}
foreach ($name in $LauncherNames) {
    $launcher = Join-Path $script:ToolBin $name
    if (Test-Path $launcher) { Move-Item $launcher (Join-Path $backupBin $name) }
}

$script:CandidateInstallAttempted = $true
& uv tool install --python $PyVersion $spec
if ($LASTEXITCODE -ne 0) { Die "candidate activation failed" }

$toolBin = $script:ToolBin
$env:Path = "$toolBin;$env:Path"
$cli = Get-CliCommand
if (-not $cli) {
    Die "okto-neuron installed but was not found in $toolBin. Run 'uv tool update-shell', restart PowerShell, re-run."
}
Info "okto-neuron: $cli"

$toolRoot = $script:ToolRoot
$toolPython = Join-Path (Join-Path $toolRoot $ToolName) "Scripts\python.exe"
if (-not (Test-Path $toolPython)) {
    Die "could not locate Okto Neuron's uv-managed Python at $toolPython"
}
$installedVersion = [string](& $toolPython -c 'import importlib.metadata; print(importlib.metadata.version(''okto-neuron''))')
$installedVersion = $installedVersion.Trim()
if ($installedVersion -ne $candidateVersion) {
    Die "installed Okto Neuron $installedVersion, expected staged $candidateVersion"
}
$cliVersion = [string](Invoke-NativeCommand $cli @("--version") | Select-Object -First 1)
if ($LASTEXITCODE -eq 0 -and $cliVersion.Trim() -ne "okto-neuron $installedVersion") {
    Die "the installed okto-neuron command does not match package version $installedVersion"
}
Info "version: $installedVersion"
Invoke-AppHomeMigration $cli

# Best-effort: persist PATH into the user's profile so a NEW PowerShell
# session (next terminal, next re-run) finds okto-neuron without manual setup.
# Opt out for sandboxed/test runs that must not touch the real user PATH.
if ($env:OKTO_NEURON_NO_UPDATE_SHELL -ne "1") {
    Invoke-NativeCommand uv @("tool", "update-shell") | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Info "persisted PATH via 'uv tool update-shell'"
    } else {
        Warn "run 'uv tool update-shell' to persist PATH"
    }
}

if ($upgrade) {
    Step "Update mode - leaving your vaults, default, and LLM config untouched"
} elseif ($Vault) {
    Step "Creating vault '$Vault' (packs: $Packs)"
    if (Test-VaultHasConfig $VaultDir) {
        Info "vault already exists at $VaultDir - leaving it as-is"
        & $cli vault use $Vault | Out-Null
    } else {
        Run-Checked $cli @("vault", "create", $Vault, "--packs", $Packs, "--use")
        Info "created $VaultDir"
    }

    Step "Configuring provider and model with 'okto-neuron onboard'"
    $onboardArgs = @("onboard", "--vault", $Vault)
    if ($env:OKTO_NEURON_LLM_PROVIDER) {
        $onboardArgs += @("--provider", $env:OKTO_NEURON_LLM_PROVIDER)
    } elseif ([Console]::IsInputRedirected -or $env:OKTO_NEURON_ONBOARD_NONINTERACTIVE -eq "1") {
        if ($env:OKTO_NEURON_LLM_API_BASE -or $env:OKTO_NEURON_LLM_MODEL) {
            $onboardArgs += @("--provider", "custom")
        } else {
            $onboardArgs += @("--provider", "skip")
        }
    }
    if ($env:OKTO_NEURON_LLM_API_BASE) {
        $onboardArgs += @("--api-base", $env:OKTO_NEURON_LLM_API_BASE)
    }
    if ($env:OKTO_NEURON_LLM_MODEL) {
        $onboardArgs += @("--model", $env:OKTO_NEURON_LLM_MODEL)
    }
    if ($env:OKTO_NEURON_LLM_API_KEY_ENV) {
        $onboardArgs += @("--api-key-env", $env:OKTO_NEURON_LLM_API_KEY_ENV)
    }
    if ($env:OKTO_NEURON_LLM_SKIP_DISCOVERY -eq "1" -or $env:OKTO_NEURON_LLM_MODEL) {
        $onboardArgs += "--skip-model-discovery"
    }
    if ($env:OKTO_NEURON_LLM_ALLOW_REMOTE -eq "1") {
        $onboardArgs += @("--allow-remote-llm", "--yes")
    }
    if ([Console]::IsInputRedirected -or $env:OKTO_NEURON_ONBOARD_NONINTERACTIVE -eq "1") {
        $onboardArgs += "--non-interactive"
        Info "no interactive terminal detected - using noninteractive onboarding"
    }
    Run-Checked $cli $onboardArgs
} else {
    Step "Application-first setup"
    Info "no startup vault was requested; create, select, and configure vaults in the web UI"
}

# Tracks whether we can honestly report success at the end. Starts true -
# OKTO_NEURON_NO_SERVE=1 (never attempted) and upgrade (already known-good) are
# not failures. Only a failed health wait below flips it. Defined on ALL
# paths (Set-StrictMode) since the final block reads it unconditionally.
$serveOk = $true
$serverStarted = $false
$logPath = Join-Path (Join-Path $HomeRoot "logs") "okto-neuron-serve.log"
if ($env:OKTO_NEURON_NO_SERVE -eq "1") {
    Step "Skipping daemon start (OKTO_NEURON_NO_SERVE=1)"
} elseif ($upgrade -and -not $wasRunning) {
    Step "Preserving stopped daemon state"
    Info "the daemon was stopped before this update, so it remains stopped"
} else {
    if ($upgrade) {
        Step "Restarting the application daemon on the new version"
    } else {
        Step "Starting the Okto Neuron daemon (UI/REST :7777 + MCP :8201)"
    }
    $script:CandidateDaemonStarted = $true
    $serveArgs = @("serve", "--daemon", "--no-open")
    # The 0.0.40 daemon credential lives under its verified vault. Give the
    # successor that vault exactly once so its runtime can adopt the credential
    # into application scope without rotating connected MCP clients. Fresh and
    # already application-scoped starts remain vaultless.
    if ($script:LegacyDaemon -and $script:PreviousDaemonVault) {
        $serveArgs += @("--vault", $script:PreviousDaemonVault)
    }
    & $cli @serveArgs
    $serveExitCode = $LASTEXITCODE
    $observedCandidateProcessId = Read-ServerProcessId $DaemonPidFile
    if ($observedCandidateProcessId -gt 0) {
        $script:CandidateProcessId = $observedCandidateProcessId
    }
    if ($serveExitCode -ne 0) {
        $serveOk = $false
        Warn "daemon start command failed"
    } else {
        Info "waiting for server version $installedVersion ..."
        $serverVersion = ""
        for ($i = 0; $i -lt 60; $i++) {
            if ($script:CandidateProcessId -le 0) {
                $observedCandidateProcessId = Read-ServerProcessId $DaemonPidFile
                if ($observedCandidateProcessId -gt 0) {
                    $script:CandidateProcessId = $observedCandidateProcessId
                }
            }
            $serverVersion = Get-ServerVersion $cli
            if ($serverVersion -eq $installedVersion) { break }
            Start-Sleep -Seconds 1
        }
        if ($serverVersion -eq $installedVersion) {
            $serverStarted = $true
            Info "server: ready ($RestUrl, version $serverVersion)"
        } else {
            $serveOk = $false
            if ($serverVersion) {
                Warn "server reported version $serverVersion; installed version is $installedVersion"
            } else {
                Warn "server did not become ready within 60s"
            }
            Warn "try 'okto-neuron serve --foreground --no-open'"
            Warn "daemon log: $logPath"
        }
    }
    if (-not $serveOk -and (Test-Path $logPath)) {
        Info "last 20 lines of ${logPath}:"
        Get-Content -Path $logPath -Tail 20 | ForEach-Object { Info $_ }
    }
}

if (-not $serveOk) {
    Die "candidate daemon verification failed; the previous installation will be restored"
}
$script:ActivationCommitted = $true
$script:TransactionArmed = $false
$script:ActivationStarted = $false
$script:CandidateInstallAttempted = $false
Remove-DirectoryTree $script:BackupRoot
$script:BackupRoot = $null

# Upgrading from Marginalia: the daemon keeps its port and adopts the old
# credential, so an existing `marginalia` entry keeps working until it is
# replaced. `okto-neuron` is registered in the scope the old entry had (user or
# local) and verified as connected (Claude Code's own health check), or, when
# the daemon was intentionally left stopped, verified as written (scope, URL,
# credential), since nothing can connect yet. Only then is
# the old `marginalia` entry in that same scope removed, and only when it pointed
# at this same endpoint. If the installer fails after that removal, the trap adds
# the old entry back exactly as it was. A project entry (a shared .mcp.json file)
# is only reported.
$globalUrl = $McpUrl
$tokenFile = if (Test-Path $DaemonTokenFile) { $DaemonTokenFile } else { $LegacyDaemonTokenFile }
$authToken = if (Test-Path $tokenFile) { (Get-Content -Raw $tokenFile).Trim() } else { "" }
$script:GlobalMcpUrl = $globalUrl
$script:McpAuthToken = $authToken
$mcpWired = $false
$mcpScopeWired = ""

function Get-ScopeLabel([string]$Scope) {
    switch ($Scope) {
        "local" { return "Local" }
        "project" { return "Project" }
        default { return "User" }
    }
}

# With the daemon running, Claude's own health check must report the entry as
# Connected. With the daemon intentionally stopped (an update of a stopped
# install, or OKTO_NEURON_NO_SERVE=1) nothing can connect, so the entry is
# verified as written instead: scope, URL and this daemon's credential.
function Test-McpRegistrationVerified([string]$Output, [string]$Scope) {
    if ($serverStarted) {
        return (Test-ClaudeMcpRegistrationMatches $Output $globalUrl (Get-ScopeLabel $Scope))
    }
    return (Test-ClaudeMcpRegistrationWritten $Output $globalUrl (Get-ScopeLabel $Scope) $authToken)
}

function Register-Mcp([string]$Scope) {
    $kind = if ($serverStarted) { "connected" } else { "configured" }
    $output = (Invoke-NativeCommand claude @("mcp", "get", $CliName) -MergeStderr | Out-String)
    if ($LASTEXITCODE -eq 0) {
        if (Test-McpRegistrationVerified $output $Scope) {
            $script:mcpWired = $true
            $script:mcpScopeWired = $Scope
            Info "preserved $kind '$CliName' $Scope-scope registration"
            return $true
        }
        $existingScope = Get-ClaudeMcpRegistrationScope $output
        Warn "an existing '$CliName' Claude MCP entry is not the $kind $Scope-scope endpoint $globalUrl"
        if ($existingScope -in @("local", "project", "user")) {
            Info "resolve it with: claude mcp remove $CliName --scope $existingScope"
        } else {
            Info "inspect it with: claude mcp get $CliName"
        }
        Die "Claude MCP registration conflict; resolve the existing entry and re-run this installer"
    }
    Invoke-NativeCommand claude @("mcp", "add", "--scope", $Scope, "--transport", "http", $CliName, $globalUrl, "--header", "Authorization: Bearer $authToken") | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $output = (Invoke-NativeCommand claude @("mcp", "get", $CliName) -MergeStderr | Out-String)
        if ($LASTEXITCODE -ne 0 -or -not (Test-McpRegistrationVerified $output $Scope)) {
            Die "Claude MCP registration was added but did not verify as a $kind $Scope-scope endpoint"
        }
        $script:mcpWired = $true
        $script:mcpScopeWired = $Scope
        Info "registered and verified the app-scoped '$CliName' MCP endpoint ($Scope scope)"
        return $true
    }
    Warn "automatic Claude Code registration failed"
    return $false
}

# Called only after Register-Mcp verified the new entry (connected, or as
# written while the daemon is stopped).
function Remove-LegacyMcp([string]$Scope, [string]$LegacyOutput) {
    $pointsHere = $false
    foreach ($line in ($LegacyOutput -split "`r?`n")) {
        $fields = @($line.Trim() -split "\s+")
        if ($fields.Count -eq 2 -and $fields[0] -ceq "URL:" -and $fields[1] -ceq $globalUrl) { $pointsHere = $true }
    }
    if (-not $pointsHere) {
        Warn "the old '$LegacyCliName' $Scope-scope entry points somewhere else; it was left unchanged"
        return
    }
    Invoke-NativeCommand claude @("mcp", "remove", $LegacyCliName, "--scope", $Scope) | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Warn "could not remove the old '$LegacyCliName' $Scope-scope entry; remove it with: claude mcp remove $LegacyCliName --scope $Scope"
        return
    }
    $script:LegacyMcpRemovedScope = $Scope
    $after = (Invoke-NativeCommand claude @("mcp", "get", $LegacyCliName) -MergeStderr | Out-String)
    if ($LASTEXITCODE -eq 0 -and (Get-ClaudeMcpRegistrationScope $after) -eq $Scope) {
        Die "the old '$LegacyCliName' $Scope-scope entry is still registered after removal"
    }
    Info "removed the old '$LegacyCliName' $Scope-scope entry; '$CliName' replaces it"
}

if ($env:OKTO_NEURON_NO_MCP -eq "1") {
    Step "Skipping Claude Code wiring (OKTO_NEURON_NO_MCP=1)"
} elseif (-not $authToken) {
    Step "Claude Code wiring deferred"
    Info "start the daemon, then re-run this installer to register its authenticated MCP endpoint"
} elseif (Get-Command claude -ErrorAction SilentlyContinue) {
    $legacyMcpOutput = (Invoke-NativeCommand claude @("mcp", "get", $LegacyCliName) -MergeStderr | Out-String)
    $legacyMcpScope = if ($LASTEXITCODE -eq 0) { Get-ClaudeMcpRegistrationScope $legacyMcpOutput } else { "" }
    if ($legacyMcpScope -eq "local") {
        Step "Re-registering the Marginalia MCP entry as '$CliName' (local scope, as before)"
        if (Register-Mcp "local") { Remove-LegacyMcp "local" $legacyMcpOutput }
    } else {
        Step "Registering the authenticated MCP server with Claude Code (user scope)"
        # Okto Neuron reuses the application MCP credential across daemon restarts.
        # Preserve an existing user registration instead of deleting a working
        # integration before its replacement is proven.
        if ((Register-Mcp "user") -and $legacyMcpScope -eq "user") { Remove-LegacyMcp "user" $legacyMcpOutput }
        if ($legacyMcpScope -eq "project") {
            Warn "a project-scope '$LegacyCliName' entry (.mcp.json) was left unchanged; it still works, and you can rename it to '$CliName' in that file"
        }
    }
    if ($mcpWired -and -not $serverStarted) {
        Info "the daemon is not running, so Claude Code lists '$CliName' as failed to connect; it connects on the next '$CliName serve'"
    }
} else {
    Step "Claude Code CLI not found"
    Info "install Claude Code, then re-run this installer to register Okto Neuron"
}

# The daemon itself stays headless while the installer proves the exact version.
# Only the committed, verified application is allowed to launch a browser.
if ($serverStarted -and $env:OKTO_NEURON_NO_OPEN -ne "1") {
    Step "Opening the verified Okto Neuron application"
    if (Open-ApplicationUi "$RestUrl/") {
        Info "opened $RestUrl/"
    } else {
        Warn "browser launch is unavailable; open $RestUrl/"
    }
}

Write-Host ""
if (-not $serveOk) {
    Write-Host "Okto Neuron $installedVersion installed, but the daemon did not start correctly." -ForegroundColor Yellow
    Info "start manually: okto-neuron serve --foreground"
    Info "log      : $logPath"
    exit 1
}

if ($upgrade) {
    if ($serverStarted) {
        Write-Host "Okto Neuron $installedVersion updated and restarted." -ForegroundColor Green
    } else {
        Write-Host "Okto Neuron $installedVersion updated; daemon remains stopped." -ForegroundColor Green
    }
} elseif ($serverStarted) {
    Write-Host "Okto Neuron $installedVersion is ready." -ForegroundColor Green
} else {
    Write-Host "Okto Neuron $installedVersion installed; daemon was not started." -ForegroundColor Green
}
Info "vaults   : managed independently in the application"
Info "graph backend: chosen at vault creation, Okto Grafx by default (Ladybug or Neo4j selectable)"
if ($serverStarted) {
    Info "web UI   : $RestUrl/"
    Info "stop     : okto-neuron stop"
    if ($mcpWired) {
        Info "Claude MCP: authenticated $mcpScopeWired-scope connection registered as '$CliName'"
    }
} else {
    Info "start    : okto-neuron serve --daemon"
}
Info "update   : re-run this installer"
Remove-InstallerTemps
