# install.ps1 — One-shot setup for the Wilma School AI test client (Windows)
#
# Usage (run in PowerShell):
#   .\install.ps1              Install CLI client only (default)
#   .\install.ps1 -Full        Install CLI + parser sidecar (requires Podman & Ollama)
#
# If you get an execution policy error, run first:
#   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
#

param(
    [switch]$Full,
    [switch]$Help
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$MinPythonMajor = 3
$MinPythonMinor = 11
$VenvDir = ".venv"

# ── Helpers ───────────────────────────────────────────────────────────────────

function Write-Info  { param($msg) Write-Host "[INFO]  $msg" -ForegroundColor Cyan }
function Write-Ok    { param($msg) Write-Host "[OK]    $msg" -ForegroundColor Green }
function Write-Warn  { param($msg) Write-Host "[WARN]  $msg" -ForegroundColor Yellow }
function Write-Err   { param($msg) Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }

# ── Help ──────────────────────────────────────────────────────────────────────

if ($Help) {
    Write-Host @"
Usage: .\install.ps1 [options]

Options:
  (none)    Install CLI client only (default)
  -Full     Install CLI + parser sidecar (requires Podman & Ollama)
  -Help     Show this help

"@
    exit 0
}

$Mode = if ($Full) { "full" } else { "cli" }
Write-Info "Mode: $Mode"
Write-Host ""

# ── Step 1: Find or install Python ────────────────────────────────────────────

function Find-Python {
    # Check common Python commands
    foreach ($cmd in @("python", "python3", "py")) {
        try {
            $ver = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($ver) {
                $parts = $ver.Split(".")
                $major = [int]$parts[0]
                $minor = [int]$parts[1]
                if ($major -gt $MinPythonMajor -or ($major -eq $MinPythonMajor -and $minor -ge $MinPythonMinor)) {
                    return $cmd
                }
            }
        } catch {}
    }

    # Try the Windows Python launcher
    try {
        $ver = & py -3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($ver) {
            $parts = $ver.Split(".")
            $major = [int]$parts[0]
            $minor = [int]$parts[1]
            if ($major -gt $MinPythonMajor -or ($major -eq $MinPythonMajor -and $minor -ge $MinPythonMinor)) {
                return "py -3"
            }
        }
    } catch {}

    return $null
}

$PythonCmd = Find-Python

if ($PythonCmd) {
    $pyVersion = & $PythonCmd --version 2>&1
    Write-Ok "Found Python: $PythonCmd ($pyVersion)"
} else {
    Write-Warn "Python $MinPythonMajor.$MinPythonMinor+ not found."

    # Try winget (available on Windows 10 1709+ and Windows 11)
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Info "Installing Python via winget..."
        winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
        # Refresh PATH
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
        $PythonCmd = Find-Python
        if ($PythonCmd) {
            Write-Ok "Installed Python: $PythonCmd"
        } else {
            Write-Err "Python still not found after install. Please restart your terminal and run this script again."
        }
    } else {
        Write-Host ""
        Write-Err @"
Python $MinPythonMajor.$MinPythonMinor+ is required but not found.

Install it from: https://www.python.org/downloads/
  - Make sure to check 'Add Python to PATH' during installation!

After installing, restart PowerShell and run this script again.
"@
    }
}

# ── Step 2: Create virtual environment ────────────────────────────────────────

if (Test-Path $VenvDir) {
    Write-Info "Virtual environment already exists at $VenvDir"
} else {
    Write-Info "Creating virtual environment..."
    if ($PythonCmd -eq "py -3") {
        & py -3 -m venv $VenvDir
    } else {
        & $PythonCmd -m venv $VenvDir
    }
    Write-Ok "Created $VenvDir"
}

# Activate
$ActivateScript = Join-Path $VenvDir "Scripts\Activate.ps1"
if (-not (Test-Path $ActivateScript)) {
    Write-Err "Venv activation script not found at $ActivateScript"
}
& $ActivateScript

# ── Step 3: Install Python dependencies ──────────────────────────────────────

Write-Info "Installing Python dependencies..."
pip install --upgrade pip --quiet 2>$null
pip install -r requirements.txt --quiet
Write-Ok "Dependencies installed."

# ── Step 4: Create .env template ──────────────────────────────────────────────

if (-not (Test-Path ".env")) {
    Write-Info "Creating .env template..."
    @"
# Wilma credentials — fill these in to use the CLI
WILMA_BASE_URL=https://espoo.inschool.fi
WILMA_USERNAME=
WILMA_PASSWORD=
"@ | Set-Content -Path ".env" -Encoding UTF8
    Write-Warn "Edit .env and fill in your Wilma credentials before using the CLI."
} else {
    Write-Ok ".env file already exists."
}

# ── Step 5: Create Windows CLI wrapper ────────────────────────────────────────

$WilmaCliCmd = @"
@echo off
"%~dp0.venv\Scripts\python.exe" -m cli %*
"@
Set-Content -Path "wilma-cli.cmd" -Value $WilmaCliCmd -Encoding ASCII
Write-Ok "wilma-cli.cmd is ready."

# ── Step 6 (full mode): Podman + Ollama + parser sidecar ─────────────────────

if ($Mode -eq "full") {
    Write-Host ""
    Write-Info "── Full setup: parser sidecar + Ollama ──"
    Write-Host ""

    # Check for Podman
    $ComposeCmd = $null
    if (Get-Command podman-compose -ErrorAction SilentlyContinue) {
        $ComposeCmd = "podman-compose"
        Write-Ok "Found podman-compose"
    } elseif (Get-Command podman -ErrorAction SilentlyContinue) {
        $ComposeCmd = "podman compose"
        Write-Ok "Found podman compose"
    } else {
        Write-Warn "Podman not found."
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Write-Info "Installing Podman via winget..."
            winget install RedHat.Podman --accept-package-agreements --accept-source-agreements
            pip install podman-compose
            $ComposeCmd = "podman-compose"
            Write-Ok "Installed podman-compose"
            Write-Warn "You may need to run 'podman machine init' and 'podman machine start' before using containers."
        } else {
            Write-Err @"
Podman not found. Install it from: https://podman.io/docs/installation
Then run: podman machine init && podman machine start
"@
        }
    }

    # Check for Ollama
    if (Get-Command ollama -ErrorAction SilentlyContinue) {
        Write-Ok "Found Ollama"
    } else {
        Write-Warn "Ollama not found. Install it from: https://ollama.com/download"
        Write-Warn "The parser sidecar will start but LLM parsing won't work without Ollama."
    }

    # Pull the LLM model
    $Model = "hf.co/mradermacher/Llama-Poro-2-8B-Instruct-GGUF:Q4_K_M"
    if (Get-Command ollama -ErrorAction SilentlyContinue) {
        $list = ollama list 2>$null
        if ($list -match "Llama-Poro") {
            Write-Ok "LLM model already pulled."
        } else {
            Write-Info "Pulling LLM model (this may take a while on first run)..."
            try {
                ollama pull $Model
            } catch {
                Write-Warn "Failed to pull model. You can pull it later with: ollama pull $Model"
            }
        }
    }

    # Build and start the parser sidecar
    Write-Info "Building and starting parser sidecar..."
    Invoke-Expression "$ComposeCmd up -d --build"
    Write-Ok "Parser sidecar is running on http://localhost:8090"
}

# ── Done ──────────────────────────────────────────────────────────────────────

Write-Host ""
Write-Ok "Installation complete! (mode: $Mode)"
Write-Host ""

if ($Mode -eq "cli") {
    Write-Host @"
  Next steps:
    1. Edit .env with your Wilma credentials
    2. Run:  .\wilma-cli.cmd children

  Available commands:
    .\wilma-cli.cmd children              List children on the account
    .\wilma-cli.cmd schedule              Show this week's schedule
    .\wilma-cli.cmd exams                 List upcoming exams
    .\wilma-cli.cmd messages              Show recent messages
    .\wilma-cli.cmd homework              Show recent homework

  Add --json to any command for raw JSON output.
  Add --help for more options.

  For full setup (parser + LLM), run: .\install.ps1 -Full

"@
} else {
    Write-Host @"
  Full stack running:
    - wilma-cli.cmd     -> Wilma API client
    - wilma-parser      -> http://localhost:8090 (LLM event extraction)
    - Ollama            -> http://localhost:11434 (local LLM)

  Next steps:
    1. Edit .env with your Wilma credentials
    2. Test the CLI:  .\wilma-cli.cmd children
    3. Copy custom_components\wilma_school_ai\ into your HA config
    4. Restart Home Assistant and configure the integration

  Manage the parser:
    $ComposeCmd logs -f        Follow parser logs
    $ComposeCmd restart        Restart the parser
    $ComposeCmd down           Stop the parser

"@
}
