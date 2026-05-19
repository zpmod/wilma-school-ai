# Wilma School AI — Home Assistant Integration

> **Extract calendar events from Finnish school messages using a local LLM — no cloud, no data leaves your network.**

A Home Assistant custom integration that connects to [Wilma](https://www.visma.fi/wilma/) (Finnish school communication platform) and uses a local LLM to automatically extract dates, events, and deadlines from teacher messages into your HA calendar.

## Features

- **Wilma Integration**: Schedule, homework, exams, messages, notifications
- **Local LLM Parsing**: Extracts events from free-text Finnish school messages
- **Privacy-First**: All processing happens locally — message bodies never leave your network
- **Async Processing**: Fire-and-forget parsing with background polling (works on Raspberry Pi)
- **Smart Deduplication**: SHA-based caching, cross-message correlation, revision tracking
- **HACS Compatible**: Install via HACS or manual copy
- **Standalone CLI**: Test without Home Assistant — just Python and a Wilma account

## Quick Start — CLI Only

The fastest way to try the integration. No Home Assistant, Docker, or Ollama required.

**macOS / Linux:**
```bash
git clone https://github.com/zpmod/wilma-school-ai.git
cd wilma-school-ai
./install.sh              # installs Python 3.11+, venv, dependencies
# Edit .env with your Wilma credentials
./wilma-cli children      # verify login works
./wilma-cli schedule      # this week's timetable
./wilma-cli exams         # upcoming exams
./wilma-cli messages      # recent messages
./wilma-cli homework      # recent homework
```

**Windows (PowerShell):**
```powershell
git clone https://github.com/zpmod/wilma-school-ai.git
cd wilma-school-ai
.\install.ps1             # installs Python 3.11+, venv, dependencies
# Edit .env with your Wilma credentials
.\wilma-cli.cmd children  # verify login works
.\wilma-cli.cmd schedule  # this week's timetable
```

> If you get an execution policy error, run first:
> `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`

Add `--json` to any command for machine-readable output. Use `--help` for all options.

## Full Setup — Home Assistant + LLM Parser

For the complete stack (HA integration + AI-powered message parsing):

```bash
./install.sh --full       # CLI + Podman + Ollama model pull + parser sidecar
```

On Windows:
```powershell
.\install.ps1 -Full
```

The script handles everything: Python, Podman, Ollama, model download, and starting the parser container. Once it completes:

### 1. Install the Integration into Home Assistant

**Via HACS** (once published):
- HACS → Integrations → + → search "Wilma School AI" → Install → Restart HA

**Manual:**
```bash
cp -r custom_components/wilma_school_ai/ <your-ha-config>/custom_components/
# Restart Home Assistant
```

### 2. Configure

1. Settings → Devices & Services → Add Integration
2. Search "Wilma School AI"
3. Enter your school's Wilma URL (e.g., `https://yourschool.inschool.fi`)
4. Enter your Wilma username and password
5. Select which children to track

### 3. Add Automations

Copy the example automations from [`examples/automations.yaml`](examples/automations.yaml) and the REST configuration from [`examples/configuration.yaml`](examples/configuration.yaml) into your HA config.

### 4. Verify

After the first poll cycle (default: 4 hours), check:
- `calendar.wilma_parsed` — contains extracted events
- `binary_sensor.wilma_parser_healthy` — should be "on"
- `sensor.wilma_parser_unsynced` — events pending sync

### Raspberry Pi Notes

On a Raspberry Pi 5 (8 GB), parsing takes 5–20 minutes per message. This is normal — the async architecture handles it gracefully:
- Messages are queued immediately
- Results appear in your calendar within ~20 minutes
- The hourly reconciliation catches anything missed during downtime

For Pi, use host networking in `podman-compose.override.yml`:
```yaml
services:
  wilma-parser:
    network_mode: host
    environment:
      - OLLAMA_BASE_URL=http://localhost:11434
```

## Requirements

| Component | CLI only | Full setup |
|-----------|----------|------------|
| Python 3.11+ | ✅ (auto-installed) | ✅ |
| Wilma account (`*.inschool.fi`) | ✅ | ✅ |
| Podman | — | ✅ |
| Ollama | — | ✅ |
| Home Assistant ≥ 2024.1 | — | ✅ |

Recommended LLM model: `Llama-Poro-2-8B-Instruct` (Q4_K_M quantization)

## Architecture

```
┌─────────────────────────┐   ┌──────────────────────────┐
│ Home Assistant          │   │ wilma-parser (sidecar)   │
│  • wilma_school_ai      │──▶│  FastAPI + SQLite cache  │
│  • automations          │◀──│  POST /parse (async)     │
│  • calendar entities    │   │  GET /events/unsynced    │
└─────────────────────────┘   └────────────┬─────────────┘
                                           │
                              ┌─────────────▼─────────────┐
                              │ Ollama (local)            │
                              │  Llama-Poro-2-8B Q4_K_M   │
                              └───────────────────────────┘
```

The CLI client (`wilma-cli`) talks directly to Wilma and works independently of the parser/HA stack.

## CLI Commands

| Command | Description |
|---------|-------------|
| `./wilma-cli children` | List children linked to your account |
| `./wilma-cli schedule` | Show this week's timetable |
| `./wilma-cli exams` | List upcoming exams |
| `./wilma-cli messages` | Show recent messages |
| `./wilma-cli homework` | Show recent homework entries |

Options: `--child ID` (select child), `--days N` (range), `--json` (raw output), `--limit N` (messages)

## Security & Credential Storage

**Home Assistant integration**: Credentials are entered via the UI config flow and stored in HA's internal `.storage/core.config_entries` — managed by Home Assistant, not a user-editable file.

**CLI client**: Reads credentials from a `.env` file in the project root. This file is:
- Listed in `.gitignore` (never committed)
- Created by you during setup (from `.env.example`)
- Plaintext on disk — protect with file permissions:
  ```bash
  chmod 600 .env
  ```

**Threat model**: This is a self-hosted, single-user system on a private network. The primary risk is accidental credential exposure via git. Mitigations:
- `.gitignore` excludes `.env`, `secrets.yaml`, and all database files
- CI PII scanner (`scripts/scan_pii.py`) catches credentials in committed files
- Pre-commit hook runs the PII scanner locally before each commit

If you need stronger credential protection (shared machines, remote access), consider exporting credentials as environment variables from your shell profile instead of using a `.env` file.

## Documentation

- [How It Works — Technical Deep Dive](docs/how-it-works.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Example Automations](examples/)

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgments

- [otoivanen/wilma-ha](https://github.com/otoivanen/wilma-ha) — original Wilma HA integration
- [Llama-Poro-2-8B](https://huggingface.co/LumiOpen/Llama-Poro-2-8B-Instruct) — Finnish language model by LumiOpen
- [Ollama](https://ollama.com/) — local LLM runtime
