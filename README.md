# Pinboard

A local-first personal pinning & streams system. Combine **intentional focus** (pins) with **emergent discovery** (engagement-based lab scoring).

## Install

```bash
pip install -e .
pinboard init
```

Add API keys to `~/.pinboard/config.toml`:

```toml
openai_api_key = "sk-..."
anthropic_api_key = "sk-ant-..."
```

## 2-minute walkthrough

```bash
# 1. Add a stream
pinboard add https://example.com/article --title "My Article"
# → Added stream [url] 'My Article'  id=<uuid>

# 2. Pin it
pinboard pin <stream_id>
# → prompts for a pin note, then confirms

# 3. Add another related stream — auto-connection is suggested
pinboard add https://example.com/related
# → 1 connection suggestion(s) generated. Run: pinboard connections --pending

# 4. Review and confirm the connection
pinboard connections --pending
pinboard confirm <conn_id>

# 5. Check the lab (emerging unpinned streams by open frequency)
pinboard open <stream_id>   # records an open event
pinboard lab -n 10
```

## CLI Reference

```
pinboard init
pinboard add <source> [--title T] [--note N] [--cache]
pinboard ls [--pins-only] [-l] [-j] [-P] [-s FIELDS] [-n N]
pinboard pin <id> [--note N]
pinboard unpin <id|slot>
pinboard edit <id>
pinboard edit pin <id|slot>
pinboard open <id>
pinboard connections [--pending] [-l] [-j] [-P] [-s FIELDS] [-n N]
pinboard confirm <conn_id>
pinboard reject <conn_id>
pinboard link <stream_id> <pin_id> [--note N]
pinboard lab [-n N] [-l] [-j] [-P] [-s FIELDS]
pinboard why <id>
pinboard search <query> [-n N] [-l] [-j] [-P] [-s FIELDS]
pinboard graph [--pin ID] [-j] [-P] [-s FIELDS]
pinboard export [--format json]
```

## Output flags (all listing commands)

| Flag | Meaning |
|------|---------|
| `-l, --link` | One URL/path per line (for piping) |
| `-j, --json` | JSON output |
| `-P, --pretty` | Pretty-print JSON |
| `-s FIELDS` | Comma-separated field selection |
| `-n N` | Limit to N results |

```bash
# Open top 5 lab streams in browser
pinboard lab -n 5 --link | xargs open

# Export titles and scores as pretty JSON
pinboard lab -n 20 --json --select title,score --pretty
```

## Storage

Everything lives in `~/.pinboard/`:

```
~/.pinboard/
├── config.toml        # API keys, thresholds
├── pinboard.db        # SQLite: streams, pins, connections, events
└── artifacts/         # PDFs, images, cached HTML
```

Move this directory to migrate to another machine.

## Design

- **Structured data for truth** — SQLite tables with enforced schemas
- **Documents for evidence** — files on disk, DB points at them
- **Events for causality** — append-only event log; scores derived on read, never stored
- **Graphs for relationships** — pin↔stream connection graph with weights

Lab scores use exponential time-decay: `score = Σ exp(-age_days / half_life_days)` with configurable `half_life_days` (default 14).
