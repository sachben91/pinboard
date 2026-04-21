# Pinboard

A local-first personal knowledge tool that combines **intentional focus** (pins) with **emergent discovery** (engagement scoring + daily AI digest).

You curate 3 pinned items per channel representing your current thinking. Everything else flows through as streams — URLs, PDFs, notes. Claude detects conceptual connections between new streams and your pins, and sends you a daily Telegram digest: one unread pick from your streams + two related web discoveries it found.

Nothing leaves your machine except API calls. All data lives in `~/.pinboard/`.

---

## What you need

| Requirement | What for | Required? |
|---|---|---|
| Python 3.9+ | Running the tool | Yes |
| Anthropic API key | Claude connection detection + digest curation | Yes |
| Telegram bot token + chat ID | Daily digest delivery | Optional |
| OpenAI API key | Embeddings (for `search`) | Optional |

Anthropic key → [console.anthropic.com](https://console.anthropic.com)

---

## Install

```bash
git clone https://github.com/sachben91/pinboard
cd pinboard
pip install -e .
```

---

## First-time setup

**1. Initialize**

```bash
python3 -m pinboard.cli init
```

This creates `~/.pinboard/` with a SQLite database and a `default` channel.

**2. Create your config**

```bash
cat > ~/.pinboard/config.toml << 'EOF'
anthropic_api_key = "sk-ant-..."

# Optional — for semantic search
openai_api_key = "sk-..."

# Optional — for local embeddings instead of OpenAI
# embedding_provider = "local"
# embedding_model = "all-MiniLM-L6-v2"

# Optional — for daily Telegram digest
telegram_bot_token = "..."
telegram_chat_id = "..."

# Tuning (these are the defaults)
connection_threshold = 0.55
half_life_days = 14
EOF
```

**3. Set up Telegram (optional)**

1. Message [@BotFather](https://t.me/botfather) → `/newbot` → copy the token
2. Message your new bot once, then run:
```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -c "
import sys, json; d=json.load(sys.stdin)
print(d['result'][-1]['message']['chat']['id'])
"
```
3. Add both values to `config.toml`

---

## 5-minute walkthrough

```bash
# Create a channel for a topic you're thinking about
python3 -m pinboard.cli channel create philosophy

# Add something you've been reading
python3 -m pinboard.cli add https://example.com/article

# Pin it as your current focus (max 3 per channel)
python3 -m pinboard.cli pin <stream_id>

# Add something else — Claude will judge if it's conceptually connected
python3 -m pinboard.cli add https://example.com/related-article

# Check if a connection was suggested
python3 -m pinboard.cli connections --pending

# Confirm or reject it
python3 -m pinboard.cli confirm <conn_id>
python3 -m pinboard.cli reject <conn_id>

# See what's gaining traction in your unread pile
python3 -m pinboard.cli lab

# Preview today's digest (1 stream pick + 2 web discoveries)
python3 -m pinboard.cli digest --dry-run

# Send it to Telegram
python3 -m pinboard.cli digest
```

---

## Channels

Each channel is an isolated workspace with its own streams, pins (max 3), and lab scores. Good for separating distinct areas of focus.

```bash
python3 -m pinboard.cli channel create research
python3 -m pinboard.cli channel ls           # ▶ = active channel
python3 -m pinboard.cli channel switch default
```

All commands work on the active channel. Use `--channel <name>` to target a specific one without switching.

---

## Daily digest

Claude picks 3 things per channel each morning:

- **1 from your streams** — the most relevant unread item given your current pins
- **2 from the web** — Claude searches for related articles and explains why each is worth reading

```bash
# Preview
python3 -m pinboard.cli digest --dry-run

# Send to Telegram
python3 -m pinboard.cli digest

# Schedule daily at 8am (run once)
(crontab -l 2>/dev/null; echo "0 8 * * * /usr/bin/python3 -m pinboard.cli digest >> ~/.pinboard/digest.log 2>&1") | crontab -
```

---

## Adding content

```bash
# URL
python3 -m pinboard.cli add https://example.com

# PDF (drag the file into terminal to get its path)
python3 -m pinboard.cli add ~/Downloads/paper.pdf

# Plain note
python3 -m pinboard.cli add "interesting idea about X"

# Bulk from a file (one URL per line)
cat urls.txt | xargs -I {} python3 -m pinboard.cli add {}

# With title and note
python3 -m pinboard.cli add https://example.com --title "My Title" --note "why I saved this"
```

---

## CLI reference

```
# Channels
python3 -m pinboard.cli channel ls
python3 -m pinboard.cli channel create <name>
python3 -m pinboard.cli channel switch <name>

# Streams
python3 -m pinboard.cli add <url|path|text> [--title T] [--note N] [--channel C]
python3 -m pinboard.cli ls [--pins-only] [--channel C]
python3 -m pinboard.cli open <id>
python3 -m pinboard.cli edit <id>
python3 -m pinboard.cli why <id>

# Pins (max 3 per channel)
python3 -m pinboard.cli pin <id> [--note N]
python3 -m pinboard.cli unpin <id|slot>
python3 -m pinboard.cli edit pin <id|slot>

# Connections
python3 -m pinboard.cli connections [--pending]
python3 -m pinboard.cli confirm <conn_id>
python3 -m pinboard.cli reject <conn_id>
python3 -m pinboard.cli link <stream_id> <pin_id> [--note N]

# Discovery
python3 -m pinboard.cli lab [-n N]
python3 -m pinboard.cli search "<query>"
python3 -m pinboard.cli graph [--pin ID]

# Digest
python3 -m pinboard.cli digest [--dry-run]

# Export
python3 -m pinboard.cli export [--format json] [--channel C]
```

### Output flags (all listing commands)

| Flag | Meaning |
|---|---|
| `-l, --link` | One URL/path per line — pipe to `xargs open` |
| `-j, --json` | JSON output |
| `-P, --pretty` | Pretty-print JSON |
| `-s FIELDS` | Comma-separated field selection |
| `-n N` | Limit results |

```bash
# Open top 5 lab items in browser
python3 -m pinboard.cli lab -n 5 --link | xargs open

# Export titles and scores as JSON
python3 -m pinboard.cli lab --json --select title,score --pretty
```

---

## Claude Code skill

If you use [Claude Code](https://claude.ai/code), open this project directory and use `/pinboard` to manage everything with natural language:

```
/pinboard add https://example.com
/pinboard pin the last one I added
/pinboard show my pending connections
/pinboard create a channel called research
/pinboard what's gaining traction in my lab
```

---

## Storage

```
~/.pinboard/
├── config.toml       # API keys and settings
├── pinboard.db       # SQLite: streams, pins, connections, events
├── active_channel    # Currently active channel id
├── digest.log        # Cron digest logs
└── artifacts/        # Copied PDFs, images, cached HTML
```

Move `~/.pinboard/` to another machine to fully migrate.

---

## Local embeddings (no OpenAI key needed)

```bash
pip install sentence-transformers
```

Add to `config.toml`:
```toml
embedding_provider = "local"
embedding_model = "all-MiniLM-L6-v2"
```

The model (~90MB) downloads automatically on first use. Enables semantic `search` and improves connection detection fallback.

---

## Design principles

- **Structured data for truth** — pins, streams, connections in SQLite with enforced schemas
- **Documents for evidence** — files on disk; the DB points at them
- **Events for causality** — every action is an append-only event row; scores are derived on read, never stored
- **Graphs for relationships** — pin↔stream connections form a weighted directed graph

Lab scores: `score = Σ exp(-age_days / half_life_days)` — configurable half-life, computed fresh every time.
