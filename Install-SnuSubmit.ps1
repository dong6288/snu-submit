<#
.SYNOPSIS
  Install SNU Submit 0.1.0-alpha.1 (offline-preview) from this extracted folder into Codex.

.DESCRIPTION
  Checks first, changes later:
    0. Package files present; this folder, its ancestors and the WHOLE existing .venv tree contain no
       symlink, junction or other reparse point (checked without following any); Codex CLI present;
       no Codex marketplace named "snu-submit" for a different folder. Any failure here stops before
       anything is created or installed. A newly created .venv tree is checked the same way before pip.
    1. .venv in this folder (an existing one is reused only if it is a real Python 3.11/3.12 venv),
       pinned dependencies from requirements.lock (--require-hashes), "snu_submit.py doctor".
    2. "codex plugin marketplace add <this folder>" unless it already points here.
    3. "codex plugin add snu-submit@snu-submit", then "codex plugin list --marketplace snu-submit --json"
       must show an installed, enabled snu-submit row from the snu-submit marketplace.
  No administrator rights, no execution-policy change, nothing deleted. The only Codex configuration
  this changes is the requested registration of the local "snu-submit" marketplace and plugin, which
  the Codex CLI persists; no other setting is touched. Files are written only inside this folder (.venv and
  Python __pycache__); pip runs with --no-cache-dir.
  After success, save your work, fully quit and reopen the Codex (ChatGPT) desktop app and start a new
  local chat (https://developers.openai.com/plugins/build/plugins); this script does not restart the app.
  Registration, the installed CLI's doctor/sample, and the app's automatic skill discovery are separate checks.
  Using SNU Submit with real eTL still requires the agent to confirm your own browser login first.

.PARAMETER PythonExe
  Optional full path to python.exe (3.11 or 3.12). Default: py launcher (3.12, then 3.11), then "python".

.PARAMETER CodexExe
  Optional Codex CLI command or full path. Default: "codex" on PATH.
#>
[CmdletBinding()]
param(
    [string]$PythonExe = "",
    [string]$CodexExe = "codex"
)

function Fail([string]$Message) {
    Write-Host "ERROR: $Message" -ForegroundColor Red
    Write-Host "Installation did not complete." -ForegroundColor Red
    exit 1
}

function Invoke-Native([string]$Exe, [string[]]$Arguments, [string]$What) {
    # stdout is captured (JSON stays parseable); stderr goes to the console. Native stderr must not
    # become a terminating PowerShell error, so the preference is relaxed inside this function only.
    $ErrorActionPreference = "Continue"
    $output = & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        $text = ($output | Out-String)
        if ($text.Length -gt 1500) { $text = $text.Substring(0, 1500) + " ..." }
        Fail "$What failed (exit $LASTEXITCODE).`n$text"
    }
    return ($output | Out-String)
}

function Get-PythonVersion([string]$Exe, [string[]]$Prefix) {
    $ErrorActionPreference = "Continue"
    try {
        $out = & $Exe @Prefix -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return ($out | Select-Object -First 1).ToString().Trim() }
    } catch { }
    return ""
}

function Test-ReparseFree([string]$Path) {
    # True when the path and every existing ancestor is not a symlink, junction or other reparse point.
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $false }
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if (-not $parent -or $parent -eq $current) { break }
        $current = $parent
    }
    return $true
}

function Test-TreeReparseFree([string]$Path) {
    # True only if no entry anywhere below $Path is a symlink, junction or other reparse point.
    # Directories are walked one level at a time and a reparse point is never entered; any error
    # (e.g. access denied) or an excessive entry count fails closed.
    try {
        $pending = New-Object System.Collections.Generic.Queue[string]
        $pending.Enqueue([IO.Path]::GetFullPath($Path))
        $seen = 0
        while ($pending.Count -gt 0) {
            $dir = New-Object IO.DirectoryInfo ($pending.Dequeue())
            foreach ($entry in $dir.EnumerateFileSystemInfos()) {
                $seen++
                if ($seen -gt 200000) { return $false }
                if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $false }
                if ($entry.Attributes -band [IO.FileAttributes]::Directory) { $pending.Enqueue($entry.FullName) }
            }
        }
    } catch { return $false }
    return $true
}

function Test-JsonObject($Value) {
    return ($null -ne $Value) -and ($Value.GetType().FullName -eq "System.Management.Automation.PSCustomObject")
}

function Get-MarketplaceAction([string]$JsonText, [string]$PackageRoot) {
    # "add", "reuse" (same name already points to this folder), "conflict" or "invalid".
    # Requires a JSON object whose "marketplaces" is an array of objects; anything else is "invalid".
    if (-not $JsonText -or -not $JsonText.TrimStart().StartsWith("{")) { return "invalid" }  # top level must be an object
    try { $doc = $JsonText | ConvertFrom-Json -ErrorAction Stop } catch { return "invalid" }
    if (-not (Test-JsonObject $doc) -or -not ($doc.PSObject.Properties.Name -contains "marketplaces")) { return "invalid" }
    $rows = $doc.marketplaces
    if (-not ($rows -is [System.Array])) { return "invalid" }
    $same = @()
    foreach ($row in $rows) {
        if (-not (Test-JsonObject $row)) { return "invalid" }
        if (($row.PSObject.Properties.Name -contains "name") -and ($row.name -is [string]) -and $row.name -ceq "snu-submit") { $same += ,$row }
    }
    if ($same.Count -eq 0) { return "add" }
    foreach ($row in $same) {
        if (-not ($row.PSObject.Properties.Name -contains "root") -or -not ($row.root -is [string]) -or -not $row.root) { return "conflict" }
        try { $full = [IO.Path]::GetFullPath($row.root).TrimEnd('\', '/') } catch { return "conflict" }
        if ($full -ine $PackageRoot.TrimEnd('\', '/')) { return "conflict" }
    }
    return "reuse"
}

function Test-InstalledIdentity([string]$JsonText) {
    # Exact structured check: an "installed" row with name and marketplaceName "snu-submit",
    # installed = true and enabled = true (JSON booleans). Nothing else counts.
    # The document must be a JSON object with "installed" and "available" arrays.
    if (-not $JsonText -or -not $JsonText.TrimStart().StartsWith("{")) { return $false }
    try { $doc = $JsonText | ConvertFrom-Json -ErrorAction Stop } catch { return $false }
    if (-not (Test-JsonObject $doc)) { return $false }
    $keys = $doc.PSObject.Properties.Name
    if (-not (($keys -contains "installed") -and ($keys -contains "available"))) { return $false }
    $installedRows = $doc.installed
    $availableRows = $doc.available
    if (-not ($installedRows -is [System.Array]) -or -not ($availableRows -is [System.Array])) { return $false }
    foreach ($row in $installedRows) {
        if (-not (Test-JsonObject $row)) { continue }
        $keys = $row.PSObject.Properties.Name
        if (-not (($keys -contains "name") -and ($keys -contains "marketplaceName") -and ($keys -contains "installed") -and ($keys -contains "enabled"))) { continue }
        if (($row.name -is [string]) -and $row.name -ceq "snu-submit" -and ($row.marketplaceName -is [string]) -and
            $row.marketplaceName -ceq "snu-submit" -and ($row.installed -is [bool]) -and $row.installed -and
            ($row.enabled -is [bool]) -and $row.enabled) { return $true }
    }
    return $false
}

# Dot-sourcing (". .\Install-SnuSubmit.ps1") only defines the functions above; nothing is installed.
if ($MyInvocation.InvocationName -eq '.') { return }

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$Skill = Join-Path $Root "plugins\snu-submit\skills\snu-submit"
$Lock = Join-Path $Skill "requirements.lock"
$Cli = Join-Path $Skill "scripts\snu_submit.py"
$Venv = Join-Path $Root ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
$VenvCfg = Join-Path $Venv "pyvenv.cfg"
foreach ($required in @((Join-Path $Root ".agents\plugins\marketplace.json"),
                        (Join-Path $Root "plugins\snu-submit\.codex-plugin\plugin.json"), $Lock, $Cli)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { Fail "package file missing: $required (extract the whole ZIP first)" }
}
Write-Host "SNU Submit package folder: $Root"

# --- stage 0: checks only, nothing is changed ---
if (-not (Test-ReparseFree $Root)) { Fail "the package folder or one of its parents is a symlink/junction/reparse point (e.g. a synced or redirected folder). Extract to a plain local folder." }
$venvExists = Test-Path -LiteralPath $Venv
if ($venvExists) {
    if (-not (Test-ReparseFree $Venv)) { Fail "$Venv is a symlink/junction/reparse point. It was not changed." }
    if (-not (Test-TreeReparseFree $Venv)) {
        Fail "$Venv contains a symlink/junction/reparse point (or could not be fully inspected). It was not changed and its Python was not run."
    }
    $isVenv = (Test-Path -LiteralPath $Venv -PathType Container) -and (Test-Path -LiteralPath $VenvCfg -PathType Leaf) -and (Test-Path -LiteralPath $VenvPy -PathType Leaf)
    if (-not $isVenv) { Fail "$Venv exists but is not a Python virtual environment. It was not changed; move it away yourself or extract to a new folder." }
}
if (-not (Get-Command $CodexExe -ErrorAction SilentlyContinue)) {
    Fail "Codex CLI '$CodexExe' not found. Install Codex and sign in first, or pass -CodexExe <path>."
}
$listText = Invoke-Native $CodexExe @("plugin", "marketplace", "list", "--json") "codex plugin marketplace list"
$action = Get-MarketplaceAction $listText $Root
if ($action -eq "invalid") { Fail "could not read 'codex plugin marketplace list --json' output" }
if ($action -eq "conflict") { Fail "a Codex marketplace named 'snu-submit' already exists for a different folder. Nothing was changed." }

# --- stage 1: dependency environment in this package folder ---
if ($venvExists) {
    if ((Get-PythonVersion $VenvPy @()) -notin @("3.11", "3.12")) {
        Fail "$Venv is not a working Python 3.11/3.12 environment. It was not changed."
    }
    Write-Host "[1/3] Reusing existing environment: $Venv"
} else {
    $pyExe = ""; $pyPrefix = @()
    if ($PythonExe) {
        if ((Get-PythonVersion $PythonExe @()) -in @("3.11", "3.12")) { $pyExe = $PythonExe }
        else { Fail "-PythonExe is not a working Python 3.11 or 3.12: $PythonExe" }
    } else {
        $candidates = @()
        if (Get-Command "py" -ErrorAction SilentlyContinue) { $candidates += ,@("py", @("-3.12")); $candidates += ,@("py", @("-3.11")) }
        if (Get-Command "python" -ErrorAction SilentlyContinue) { $candidates += ,@("python", @()) }
        foreach ($c in $candidates) {
            if ((Get-PythonVersion $c[0] @($c[1])) -in @("3.11", "3.12")) { $pyExe = $c[0]; $pyPrefix = @($c[1]); break }
        }
        if (-not $pyExe) { Fail "Python 3.11 or 3.12 not found (the Microsoft Store 'python' alias does not count). Install it from python.org or pass -PythonExe <path>." }
    }
    Write-Host "[1/3] Creating environment: $Venv"
    Invoke-Native $pyExe (@($pyPrefix) + @("-m", "venv", $Venv)) "python -m venv" | Out-Null
    if (-not (Test-ReparseFree $Venv) -or -not (Test-TreeReparseFree $Venv)) {
        Fail "the new environment contains a symlink/junction/reparse point; pip was not run"
    }
}
Invoke-Native $VenvPy @("-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--no-cache-dir", "--require-hashes",
                        "--only-binary=:all:", "-r", $Lock) "pip install (hash-pinned requirements.lock)" | Out-Null
Invoke-Native $VenvPy @($Cli, "doctor") "snu_submit.py doctor" | Out-Null
Write-Host "      doctor: all checks ok (eTL login is not part of this check)"

# --- stage 2: local marketplace (only this package) ---
if ($action -eq "reuse") {
    Write-Host "[2/3] Marketplace 'snu-submit' already points to this folder."
} else {
    Write-Host "[2/3] Adding local marketplace: $Root"
    Invoke-Native $CodexExe @("plugin", "marketplace", "add", $Root, "--json") "codex plugin marketplace add" | Out-Null
}

# --- stage 3: plugin and installed identity ---
Write-Host "[3/3] Adding plugin snu-submit@snu-submit"
Invoke-Native $CodexExe @("plugin", "add", "snu-submit@snu-submit", "--json") "codex plugin add" | Out-Null
$plugins = Invoke-Native $CodexExe @("plugin", "list", "--marketplace", "snu-submit", "--json") "codex plugin list"
if (-not (Test-InstalledIdentity $plugins)) {
    Fail "'codex plugin list --marketplace snu-submit --json' does not show snu-submit as installed and enabled"
}

Write-Host ""
Write-Host "Installed SNU Submit 0.1.0-alpha.1 (offline-preview, alpha)." -ForegroundColor Green
Write-Host "  Package folder (keep it): $Root"
Write-Host "  Python environment:       $VenvPy"
Write-Host "  Local sample (synthetic): & `"$VenvPy`" `"$Cli`" sample --output <new folder>"
Write-Host "Next: save your work, fully quit and reopen the Codex (ChatGPT) desktop app, then start a NEW local"
Write-Host "chat and ask with `$snu-submit. Plugin registration (above) does not by itself confirm that the app has"
Write-Host "loaded the skill. If it is still missing after the restart, automatic skill loading is not confirmed; as a"
Write-Host "direct-file fallback you can point the chat to: $(Join-Path $Skill 'SKILL.md')"
Write-Host "Using real eTL still requires the agent to confirm your own browser login first; nothing is uploaded or"
Write-Host "submitted. A synthetic sample PASS is not readiness of a real assignment."
exit 0
