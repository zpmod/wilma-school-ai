#!/usr/bin/env bash
# install.sh — Setup script for Wilma School AI.
#
# Usage:
#   ./install.sh              Install CLI client only (default)
#   ./install.sh --full       Install CLI + parser sidecar + Podman setup
#
# CLI mode installs:
#   - Python 3.11+ (via brew/apt if missing)
#   - Virtual environment with requests & beautifulsoup4
#   - wilma-cli ready to use
#
# Full mode additionally:
#   - Podman (installs via brew/apt if missing)
#   - Ollama (checks availability)
#   - Pulls the LLM model
#   - Builds and starts the parser sidecar container
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=11
VENV_DIR=".venv"
MODE="cli"

# ── Parse arguments ───────────────────────────────────────────────────────────

usage() {
    echo "Usage: $0 [--full | --cli | --help]"
    echo ""
    echo "  --cli   Install CLI client only (default)"
    echo "  --full  Install CLI + parser sidecar (requires Podman & Ollama)"
    echo "  --help  Show this help"
    exit 0
}

for arg in "$@"; do
    case "$arg" in
        --full)  MODE="full" ;;
        --cli)   MODE="cli" ;;
        --help|-h) usage ;;
        *) echo "Unknown option: $arg" >&2; usage ;;
    esac
done

# ── Helpers ───────────────────────────────────────────────────────────────────

info()  { printf '\033[1;34m[INFO]\033[0m  %s\n' "$*"; }
ok()    { printf '\033[1;32m[OK]\033[0m    %s\n' "$*"; }
warn()  { printf '\033[1;33m[WARN]\033[0m  %s\n' "$*"; }
error() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

# ── Step 1: Find or install Python ────────────────────────────────────────────

find_python() {
    for cmd in python3.13 python3.12 python3.11 python3; do
        if command -v "$cmd" &>/dev/null; then
            local ver
            ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
            if [[ -n "$ver" ]]; then
                local major minor
                major=${ver%%.*}
                minor=${ver#*.}
                if (( major > MIN_PYTHON_MAJOR || (major == MIN_PYTHON_MAJOR && minor >= MIN_PYTHON_MINOR) )); then
                    echo "$cmd"
                    return 0
                fi
            fi
        fi
    done
    return 1
}

info "Mode: $MODE"
echo ""

PYTHON_CMD=""
if PYTHON_CMD=$(find_python); then
    ok "Found Python: $PYTHON_CMD ($($PYTHON_CMD --version))"
else
    warn "Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ not found."

    if [[ "$(uname)" == "Darwin" ]]; then
        if command -v brew &>/dev/null; then
            info "Installing Python via Homebrew..."
            brew install python@3.12
            PYTHON_CMD=$(find_python) || error "Python still not found after brew install."
            ok "Installed Python: $PYTHON_CMD ($($PYTHON_CMD --version))"
        else
            error "Homebrew not found. Install Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ manually:\n  https://www.python.org/downloads/"
        fi
    elif [[ "$(uname)" == "Linux" ]]; then
        if command -v apt-get &>/dev/null; then
            info "Installing Python via apt..."
            sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip
            PYTHON_CMD=$(find_python) || error "Python still not found after apt install."
            ok "Installed Python: $PYTHON_CMD ($($PYTHON_CMD --version))"
        else
            error "Could not auto-install Python. Install Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ manually."
        fi
    else
        error "Unsupported OS. Install Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ manually:\n  https://www.python.org/downloads/"
    fi
fi

# ── Step 2: Create virtual environment ────────────────────────────────────────

if [[ -d "$VENV_DIR" ]]; then
    info "Virtual environment already exists at $VENV_DIR"
else
    info "Creating virtual environment..."
    "$PYTHON_CMD" -m venv "$VENV_DIR"
    ok "Created $VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# ── Step 3: Install Python dependencies ──────────────────────────────────────

info "Installing Python dependencies..."
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
ok "Dependencies installed."

# ── Step 4: Create .env template ──────────────────────────────────────────────

if [[ ! -f .env ]]; then
    info "Creating .env template..."
    cat > .env <<'EOF'
# Wilma credentials — fill these in to use the CLI
WILMA_BASE_URL=https://espoo.inschool.fi
WILMA_USERNAME=
WILMA_PASSWORD=
EOF
    warn "Edit .env and fill in your Wilma credentials before using the CLI."
else
    ok ".env file already exists."
fi

# ── Step 5: Make CLI wrapper executable ───────────────────────────────────────

chmod +x wilma-cli
ok "wilma-cli is ready."

# ── Step 6 (full mode): Podman + Ollama + parser sidecar ─────────────────────

if [[ "$MODE" == "full" ]]; then
    echo ""
    info "── Full setup: parser sidecar + Ollama ──"
    echo ""

    # Check for Podman
    COMPOSE_CMD=""
    if command -v podman-compose &>/dev/null; then
        COMPOSE_CMD="podman-compose"
        ok "Found podman-compose"
    elif command -v podman &>/dev/null && podman compose --help &>/dev/null 2>&1; then
        COMPOSE_CMD="podman compose"
        ok "Found podman compose"
    else
        warn "Podman not found."
        if [[ "$(uname)" == "Darwin" ]] && command -v brew &>/dev/null; then
            info "Installing Podman via Homebrew..."
            brew install podman podman-compose
            podman machine init --now 2>/dev/null || true
            COMPOSE_CMD="podman-compose"
            ok "Installed podman-compose"
        elif [[ "$(uname)" == "Linux" ]] && command -v apt-get &>/dev/null; then
            info "Installing Podman via apt..."
            sudo apt-get update && sudo apt-get install -y podman python3-pip
            pip install --user podman-compose
            COMPOSE_CMD="podman-compose"
            ok "Installed podman-compose"
        else
            error "Podman not found. Install it from: https://podman.io/docs/installation"
        fi
    fi

    # Check for Ollama
    if command -v ollama &>/dev/null; then
        ok "Found Ollama: $(ollama --version 2>/dev/null || echo 'installed')"
    else
        warn "Ollama not found. Install it from: https://ollama.com/download"
        warn "The parser sidecar will start but LLM parsing won't work without Ollama."
    fi

    # Pull the LLM model (if Ollama is available)
    MODEL="hf.co/mradermacher/Llama-Poro-2-8B-Instruct-GGUF:Q4_K_M"
    if command -v ollama &>/dev/null; then
        if ollama list 2>/dev/null | grep -q "Llama-Poro"; then
            ok "LLM model already pulled."
        else
            info "Pulling LLM model (this may take a while on first run)..."
            ollama pull "$MODEL" || warn "Failed to pull model. You can pull it later with: ollama pull $MODEL"
        fi
    fi

    # Build and start the parser sidecar
    info "Building and starting parser sidecar..."
    $COMPOSE_CMD up -d --build
    ok "Parser sidecar is running on http://localhost:8090"

    # Verify parser health
    sleep 2
    if curl -sf http://localhost:8090/healthz >/dev/null 2>&1; then
        ok "Parser health check passed."
    else
        warn "Parser is starting up. Check with: curl http://localhost:8090/healthz"
    fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
ok "Installation complete! (mode: $MODE)"
echo ""

if [[ "$MODE" == "cli" ]]; then
    echo "  Next steps:"
    echo "    1. Edit .env with your Wilma credentials"
    echo "    2. Run:  ./wilma-cli children"
    echo ""
    echo "  Available commands:"
    echo "    ./wilma-cli children              List children on the account"
    echo "    ./wilma-cli schedule              Show this week's schedule"
    echo "    ./wilma-cli exams                 List upcoming exams"
    echo "    ./wilma-cli messages              Show recent messages"
    echo "    ./wilma-cli homework              Show recent homework"
    echo ""
    echo "  Add --json to any command for raw JSON output."
    echo "  Add --help for more options."
    echo ""
    echo "  For full setup (parser + LLM), run: ./install.sh --full"
else
    echo "  Full stack running:"
    echo "    • wilma-cli         → Wilma API client"
    echo "    • wilma-parser      → http://localhost:8090 (LLM event extraction)"
    echo "    • Ollama            → http://localhost:11434 (local LLM)"
    echo ""
    echo "  Next steps:"
    echo "    1. Edit .env with your Wilma credentials"
    echo "    2. Test the CLI:  ./wilma-cli children"
    echo "    3. Copy custom_components/wilma_school_ai/ into your HA config"
    echo "    4. Restart Home Assistant and configure the integration"
    echo ""
    echo "  Manage the parser:"
    echo "    $COMPOSE_CMD logs -f        Follow parser logs"
    echo "    $COMPOSE_CMD restart        Restart the parser"
    echo "    $COMPOSE_CMD down           Stop the parser"
fi
echo ""
