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

```bash
git clone https://github.com/YOUR_USERNAME/wilma-school-ai.git
cd wilma-school-ai
./install.sh              # installs Python 3.11+, venv, dependencies
# Edit .env with your Wilma credentials
./wilma-cli children      # verify login works
./wilma-cli schedule      # this week's timetable
./wilma-cli exams         # upcoming exams
./wilma-cli messages      # recent messages
./wilma-cli homework      # recent homework
```

Add `--json` to any command for machine-readable output. Use `--help` for all options.

## Full Setup — Home Assistant + LLM Parser

For the complete stack (HA integration + AI-powered message parsing):

```bash
./install.sh --full       # CLI + Podman parser + Ollama model pull
```

This additionally:
- Checks for Podman (installs via brew/apt if missing)
- Pulls the Finnish LLM model (~5 GB on first run)
- Builds and starts the parser sidecar container

Then:
1. Copy `custom_components/wilma_school_ai/` into your HA `config/custom_components/`
2. Restart Home Assistant
3. Add the integration via Settings → Devices & Services → Add Integration → "Wilma School AI"

See [docs/quickstart.md](docs/quickstart.md) for detailed instructions.

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
│ Home Assistant           │   │ wilma-parser (sidecar)   │
│  • wilma_school_ai      │──▶│  FastAPI + SQLite cache   │
│  • automations          │◀──│  POST /parse (async)      │
│  • calendar entities    │   │  GET /events/unsynced     │
└─────────────────────────┘   └────────────┬─────────────┘
                                           │
                              ┌─────────────▼─────────────┐
                              │ Ollama (local)             │
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

## Multi-Child Support

If you have multiple children on one Wilma account, the parser supports isolating events per child using the `child_id` parameter.

### How it works

1. **POST /parse** — include `"child_id": "child1"` in the request body (defaults to `"default"`)
2. **GET /events** — append `?child_id=child1` to retrieve only that child's events
3. **GET /events/unsynced** — same filter applies

All existing data and single-child setups continue to work unchanged (`child_id` defaults to `"default"`).

### HA automation example

Pass `child_id` from the Wilma event trigger (requires the Wilma integration to include a `child` field in `wilma_new_message` events):

```yaml
action:
  - service: rest_command.parse_wilma_message
    data:
      message_id: "{{ trigger.event.data.id }}"
      sent: "{{ trigger.event.data.sent }}"
      sender: "{{ trigger.event.data.sender }}"
      subject: "{{ trigger.event.data.subject }}"
      body: "{{ trigger.event.data.body | default('') }}"
      child_id: "{{ trigger.event.data.child | default('default') }}"
```

### Per-child dashboard cards

Create separate REST sensors per child (see [`examples/rest_sensors.yaml`](examples/rest_sensors.yaml)):

```yaml
rest:
  - resource: "http://localhost:8090/events?child_id=child1"
    scan_interval: 300
    sensor:
      - name: "Wilma Events — Child 1"
        unique_id: wilma_parser_events_child1
        value_template: "{{ value_json.events | count }}"
        json_attributes:
          - events
```

Then use `sensor.wilma_parser_events_child1` in your Lovelace cards — see [`examples/lovelace_cards.yaml`](examples/lovelace_cards.yaml) for a full template.

## Documentation

- [Quick Start Guide](docs/quickstart.md)
- [How It Works — Technical Deep Dive](docs/how-it-works.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Example Automations](examples/)

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgments

- [otoivanen/wilma-ha](https://github.com/otoivanen/wilma-ha) — original Wilma HA integration
- [Llama-Poro-2-8B](https://huggingface.co/LumiOpen/Llama-Poro-2-8B-Instruct) — Finnish language model by LumiOpen
- [Ollama](https://ollama.com/) — local LLM runtime
