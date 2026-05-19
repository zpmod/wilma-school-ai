# How It Works — Technical Deep Dive

> This document explains the architecture, design decisions, and implementation details of Wilma School AI. Written for developers and the curious.

## Table of Contents

- [The Problem](#the-problem)
- [Why Local LLM?](#why-local-llm)
- [Architecture Overview](#architecture-overview)
- [Research Journey](#research-journey)
- [Key Design Decisions](#key-design-decisions)
- [Prompt Engineering](#prompt-engineering)
- [Performance on Raspberry Pi 5](#performance-on-raspberry-pi-5)
- [Lessons Learned](#lessons-learned)
- [Post-LLM Processing Pipeline](#post-llm-processing-pipeline)
- [Entity Summary](#entity-summary)

## The Problem

Finnish schools use [Wilma](https://www.visma.fi/wilma/) for teacher–parent communication. Teachers send free-text messages in Finnish containing dates, events, deadlines, and action items buried in natural language paragraphs. Parents must manually read each message, identify dates, and add them to their calendar.

**Example message** (synthetic):
> "Hei kotiväki! Ensi viikolla on paljon ohjelmaa. Tiistaina 21.5. menemme retkelle Korkeasaareen. Muista antaa lapselle eväät ja sateenvarjo. Perjantaina 24.5. on kevätjuhla klo 10:00-12:00 koulun salissa. Vanhemmat ovat tervetulleita!"

A human reads this and extracts: "Zoo trip 21.5." and "Spring celebration 24.5. 10:00-12:00". I wanted a computer to do the same.

## Why Local LLM?

1. **Privacy**: School messages contain children's names, grades, and personal information. Cloud APIs (GPT-4, Claude) would send this data to third parties.
2. **Cost**: Commercial APIs charge per token. A family receives 3–10 messages/week — not worth a subscription.
3. **Latency tolerance**: School messages aren't time-critical. A 5–20 minute parse time on a Raspberry Pi is perfectly acceptable.

## Architecture Overview

```
Wilma Server (*.inschool.fi)
        │
        │ HTTPS polling (every 4h)
        ▼
┌──────────────────────────────┐
│ Home Assistant               │
│  wilma_school_ai integration │
│  • Polls messages/schedule   │
│  • Fires wilma_new_message   │
│  • Automations trigger parse │
└──────────────┬───────────────┘
               │ HTTP POST /parse
               ▼
┌───────────────────────────────┐
│ wilma-parser (FastAPI sidecar)│
│  • SHA256 body cache          │
│  • Prompt engineering         │
│  • Date post-processing       │
│  • SQLite event store         │
└──────────────┬────────────────┘
               │ Ollama API
               ▼
┌──────────────────────────────┐
│ Ollama                       │
│  Llama-Poro-2-8B-Instruct    │
│  (Finnish language model)    │
└──────────────────────────────┘
```

## Research Journey

This section documents the full development path — what worked, what didn't, and how the system evolved over ~4 weeks from research to production deployment.

### Starting point: fork vs build from scratch

The first decision was whether to build a Wilma client from scratch using [OpenWilma](https://github.com/OpenWilma) documentation (community-maintained endpoint wiki), or fork an existing HA integration.

I chose to fork [otoivanen/wilma-ha](https://github.com/otoivanen/wilma-ha) because it had already solved the hardest problems:
- **Login flow**: Wilma uses a custom cookie-based session (`Wilma2SID`) with redirect-based auth that is non-trivial to implement correctly (session reuse bugs, CSRF token dance)
- **Child auto-discovery**: Parses the home page HTML for `/!{child_id}/` links
- **Exam scraping**: HTML table parsing from `/!{child}/exams/calendar`
- **Message fetching**: JSON list API + per-message HTML body fetch

What was missing: schedule, homework, and — the big one — extracting calendar events from free-text messages.

### Key discovery: the `/overview` endpoint

The schedule was supposed to come from a documented export endpoint. During DevTools capture I discovered that `GET /!{child_id}/overview` returns a comprehensive JSON blob containing the entire school year — **including homework data** embedded within each course group. One endpoint, two features, zero extra HTTP calls.

### Model selection: why Llama-Poro-2-8B

I benchmarked multiple models against 9 real school messages (hand-labelled):

| Model | Quant | Precision | Recall | F1 | JSON valid | Notes |
|---|---|---|---|---|---|---|
| GPT-4 (baseline) | — | 95% | 97% | 96% | 100% | Cloud — privacy violation |
| **Llama-Poro-2-8B** | **Q4_K_M** | **83.3%** | **88.2%** | **85.7%** | 88.9% | **Winner** |
| Llama-Poro-2-8B | Q6_K | 81.2% | 68.4% | 74.3% | 100% | Worse recall, more VRAM |
| Mistral 7B | Q4_K_M | ~71% | ~65% | ~68% | — | Poor Finnish understanding |
| Llama 3 8B | Q4_K_M | ~75% | ~72% | ~73% | — | Missed Finnish date formats |

[Llama-Poro-2-8B](https://huggingface.co/LumiOpen/Llama-Poro-2-8B-Instruct) (by LumiOpen / University of Turku / Silo AI) was specifically trained on Finnish text. It handles Finnish date formats (`21.5.`, `pe 24.5.`, `ensi viikon tiistaina`) reliably where other models fail.

**Counter-intuitive result**: Q4_K_M (smaller quantization) **outperformed** Q6_K on recall. The only downside was one JSON parse failure in 9 messages (88.9% vs 100% validity), easily fixed with a one-shot retry.

**Size constraint**: The model must fit a Raspberry Pi 5 with 8 GB RAM alongside Home Assistant and a kiosk Chromium browser. Q4_K_M at ~4.9 GB leaves ~2.5 GB headroom. Q8_0 (8.5 GB) was ruled out entirely.

### Prompt engineering iterations

The prompt went through two major versions:

**v1** — basic instruction prompt:
- Simple "extract events from Finnish text" instruction
- No few-shot examples
- No date-resolution rules
- Result: missed events, wrong years, subject-line false positives

**v2** — production prompt (current):
- Hard year-resolution rule: never return past school years
- 4 few-shot examples from the most-failed real cases
- Explicit negative examples for viikkoviesti subject lines
- `is_week_event` flag for vague "sometime this week" items
- `date_source` classification (explicit_date, relative_today, weekday_only, etc.)
- `date_evidence` field — verbatim quote from body proving date choice
- Stated preference: "Yliesiintyminen on huonompi virhe kuin alipoiminta" (over-extraction worse than under-extraction)

**Key insight (temp=0)**: At temperature 0 (required for consistency), minor prompt wording changes have essentially zero effect on the 8B model. What DOES work is adding structured data to the **user message** (see Date Inventory Hint below).

### The Date Inventory Hint breakthrough

The single biggest improvement to recall came from appending a deterministic list of dates found in the message body:

```
[Rungossa havaitut päivämäärät: 21.5., 24.5., 28.5.]
```

This is a pure regex extraction — no LLM needed. But it helps the model "attend" to dates that are buried deep in long paragraphs (viikkoviesti messages can be 2-3 KB). Recall improved measurably with zero regressions.

### The async pivot: from sync to fire-and-forget

On a development Mac, the model parses a message in 5-30 seconds. On a Raspberry Pi 5, the same message takes **5–20 minutes** (the model runs at ~3.4 tokens/second on CPU). The original synchronous `POST /parse → wait → return events` architecture was completely unworkable on Pi.

The solution: fire-and-forget architecture.
1. HA sends `POST /parse` with `wait=false` → parser returns immediately
2. Parser processes in background (no timeout pressure)
3. HA polls `GET /events/unsynced` every 3 minutes
4. When events appear, HA creates calendar entries and marks them synced

Combined with an hourly reconciliation automation (re-sends all known messages — idempotent via SHA cache), this handles parser downtime, missed events, and deployment windows gracefully.

### Pi deployment gotchas

Two platform-specific issues hit us on the Pi:

1. **Container networking**: Both containers use `network_mode: host` on Pi (required for Ollama access on localhost). This means container DNS names don't resolve — URLs must use `localhost:8090`, not `wilma-parser:8090`. Opposite of the dev environment (bridge network).

2. **SQLite WAL on macOS Podman**: Volume-mounted SQLite with WAL mode doesn't persist writes to the host filesystem on macOS (virtiofs issue). Diagnosed by seeing 0 events on host while the container reported 25. Not an issue on Linux/Pi.

### Timeline

| Date | Milestone |
|---|---|
| 2026-04-21 | Research complete — decision to fork wilma-ha |
| 2026-04-21 | Live on production Wilma instance — exams, messages, schedule, homework working |
| 2026-04-21 | Model selection — Poro-2-8B Q4_K_M wins benchmark |
| 2026-04-21 | Parser service v1 — FastAPI + SQLite + filter + dateguard |
| 2026-04-22 | Dashboard views complete — custom Lovelace cards |
| 2026-05-05 | Cross-message correlation + time hints |
| 2026-05-19 | Async fire-and-forget architecture + Pi deployment |
| 2026-05-19 | First production batch: 19 messages → 25 calendar events extracted |
| 2026-05-19 | Public repo (`wilma-school-ai`) scaffolded + CLI client |

## Performance on Raspberry Pi 5

| Metric | Value |
|---|---|
| Model size | 4.9 GB (Q4_K_M) |
| Token rate | ~3.4 tok/s (CPU only) |
| Short message | ~2.5 min |
| Long viikkoviesti | ~15 min |
| Prompt eval | ~14 min (3600 token prompt) |
| RAM usage | ~5.5 GB (model + FastAPI + SQLite) |
| Free headroom | ~2.5 GB for HA + system |

The async architecture means this is perfectly acceptable — messages arrive at most a few times per week, and a 15-minute delay for calendar events is fine.

## Key Numbers

| Metric | Value |
|---|---|
| F1 score (Poro-2-8B Q4_K_M) | 85.7% |
| Precision | 83.3% |
| Recall | 88.2% |
| JSON validity | 88.9% (1 failure in 9, fixed by retry) |
| Messages benchmarked | 9 real Wilma messages |
| Events extracted (production) | 25 from 19 messages |
| Token rate (Pi 5, CPU) | ~3.4 tok/s |
| Parse time range | 2.5–15 min per message |
| Model size on disk | 4.9 GB |
| Polling interval (Wilma) | 4 hours |
| Event sync interval (HA ↔ parser) | 3 minutes |
| Reconciliation frequency | Hourly + on startup |
| Total development time | ~4 weeks (research → production) |
| False positive types | 100% subject-line leaks (deterministic fix) |

## Lessons Learned

1. **Temperature must be 0**: Any temperature > 0 causes inconsistent results and false positives on the 8B model
2. **Prompt changes at temp=0 have zero effect**: The model is too deterministic — prompt wording doesn't matter once you have the right structure
3. **User-message hints work**: Adding structured data (date inventory) to the user message is the most effective way to improve recall
4. **SHA cache is essential**: Re-parsing is free, enabling aggressive reconciliation without hammering the LLM
5. **Smaller models can work**: You don't need GPT-4 for structured extraction from Finnish text — an 8B model with good Finnish training achieves 86% F1

## Post-LLM Processing Pipeline

Every LLM response passes through a deterministic pipeline before storage:

```
LLM JSON output
    │
    ├─→ filter.py ──→ Drop false positives (subject-line leaks, duplicates)
    │
    ├─→ dateguard.py ─→ Fix weekday-in-anchor date errors
    │
    ├─→ timehint.py ──→ Extract "klo HH:MM-HH:MM" patterns into notes
    │
    └─→ correlation.py → Match against existing events (corrections/enrichments)
         │
         ▼
    store.py (SQLite) → Persist with revision history
```

### Filter (`filter.py`)

Finnish viikkoviesti (weekly newsletter) messages list each subject's plan for the week. These are NOT calendar events:
- "Math: Ch 31-34" → dropped
- "PE: indoor sports" → dropped
- "Finnish: tietotekstin kirjoittaminen" → dropped

The filter uses regex patterns to identify these subject-line entries and removes them. In benchmarks, this eliminated all 3 false positives.

### DateGuard (`dateguard.py`)

The LLM classifies each event's date source but sometimes resolves weekdays incorrectly. Example:

> Message sent Friday 2026-04-17: "Ensi viikolla... Perjantaina on lukupiknik"

The LLM might return 2026-04-17 (send date) instead of 2026-04-24 (next Friday). DateGuard:
1. Finds the week-anchor phrase ("ensi viikolla" = next week)
2. Maps the Finnish weekday name to an ISO day number
3. Snaps to the correct date in the anchored week

Only events with `date_source` = `weekday_only` or `inferred_weekday_in_anchor` are corrected. Explicit dates are never touched.

### Correlation (`correlation.py`)

Schools often send updates about previously announced events. The correlation engine:
1. Normalizes event titles (case-fold, remove hyphens/spaces: "Unicef-kävely" → "unicefkävely")
2. Matches against existing events in the database
3. Classifies changes as `correction` (date changed) or `enrichment` (notes added)
4. Records revision history for audit trail

## Entity Summary

| Entity | Type | Description |
|---|---|---|
| `sensor.wilma_<child>_exams` | Sensor | Upcoming exam count + details |
| `sensor.wilma_<child>_messages` | Sensor | Unread message count + content |
| `sensor.wilma_<child>_homework` | Sensor | Recent homework items |
| `calendar.wilma_<child>_schedule` | Calendar | Daily lesson schedule |
| `calendar.wilma_parsed` | Calendar | LLM-extracted events |
| `binary_sensor.wilma_parser_healthy` | Binary | Parser + Ollama health |
| `sensor.wilma_parser_unsynced` | Sensor | Events pending calendar sync |

## Further Reading

- [README — Setup Instructions](../README.md) — get up and running
- [Troubleshooting](troubleshooting.md) — common issues and fixes
- [Example Automations](../examples/automations.yaml) — copy-paste automation YAML
- [Example Lovelace Cards](../examples/lovelace_cards.yaml) — dashboard card examples
