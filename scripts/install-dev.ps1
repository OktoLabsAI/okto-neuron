param(
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]] $RemainingArgs
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$Installer = Join-Path $RepoRoot "scripts/install-dev.py"

if ($env:PYTHON) {
  & $env:PYTHON $Installer @RemainingArgs
  exit $LASTEXITCODE
}

foreach ($Candidate in @("py", "python", "python3")) {
  $Command = Get-Command $Candidate -ErrorAction SilentlyContinue
  if ($Command) {
    if ($Candidate -eq "py") {
      & $Command.Source -3 $Installer @RemainingArgs
    } else {
      & $Command.Source $Installer @RemainingArgs
    }
    exit $LASTEXITCODE
  }
}

Write-Error "Python 3 not found on PATH."
exit 1
