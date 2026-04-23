# Pinboard — Design Philosophy

## What this is

Pinboard is a personal knowledge tool built around a single idea: **the distinction between what you are reading and what you are thinking about**. Most tools collapse these. Pinboard keeps them separate, and treats the gap between them as the interesting space.

---

## Conceptual model

### Streams

A stream is anything you encounter — a URL, a PDF, an image, a note. Adding something to a stream makes no claim about its importance. It just means you noticed it. Streams accumulate freely.

The word "stream" is intentional. It implies flow, not storage. Things enter. Most will sit unexamined. A few will rise.

### Pins

A pin is a declaration of current focus. Pinning something says: *this is what I am thinking about right now*. It is not a bookmark or a favourite — it is an active frame through which the rest of the stream is filtered.

Each channel holds a maximum of three pins. This limit is not a technical constraint — it is a design constraint. If you cannot identify the three things you are currently focused on, the system cannot help you. The limit forces prioritisation.

Pins carry a note: *why this, why now*. That note is the most important field in the system. It is not metadata — it is the thesis. The note is what Claude reads when deciding whether a new stream is worth your attention.

When you unpin something, it is not deleted. The history of what you pinned, and when, is preserved in the events log. Intellectual trajectories matter.

### Channels

A channel is a context for focused reading. It is not a tag or a folder. It is closer to a reading session, a project, or a question you are living inside.

Streams, pins, and connections are all scoped to a channel. Switching channels switches your focus entirely. The system does not try to mix signals across channels — it assumes you want clean separation between intellectual projects.

### Connections

A connection is a relationship between a stream and a pin. The system proposes connections automatically — using Claude to detect conceptual resonance between what you just added and what you have said you are focused on. You confirm or reject each suggestion.

This human-in-the-loop step is deliberate. The system can propose; it cannot judge. Confirming a connection is an act of interpretation. It is you saying: *yes, these belong together, and I know why*.

Connections are not symmetric associations between any two streams. They are directional: a stream connects to a pin. The pin is the anchor. Everything connects to what you are thinking about, not to each other in an undifferentiated web.

### The Lab

The lab shows unpinned streams ranked by a time-decayed engagement score. Opening something adds to its score; that score decays with a 14-day half-life. The lab is the serendipity layer — it surfaces things you have touched recently and things that have sat long enough to be forgotten.

The lab does not rank by importance. It ranks by traction. A stream you keep returning to climbs. One you ignore fades.

### Skills

When you pin a stream, the system extracts a set of thematic signals from it — the intellectual terrain the piece covers. These signals are stored as pin skills. The daily digest uses skills to guide web discovery: instead of searching for the surface topic of a stream, it searches for the underlying themes your active pins are orbiting.

Skills are derived, not authored. You do not write them. They are the system's interpretation of your focus.

### The Digest

Every morning, the digest does two things per channel:

1. Picks one stream from your collection — the one most worth reading given your current pins.
2. Finds two things on the web you have not seen, related to that stream via your pin skills.

The 1+2 structure is a constraint. It is not a feed. You are not meant to consume everything the digest produces. You are meant to be offered a single thread to pull.

---

## Software design

### Local-first, cloud-secondary

The database is a single SQLite file. Everything lives there. No server, no sync protocol, no account. The tool works without a network connection for everything except the digest and connection detection.

Google Drive sync is layered on top as a convenience for running the digest from the cloud. The DB path in config falls back gracefully if the Drive mount is absent — the tool never fails to start because of a missing cloud dependency.

### Events as the record of engagement

Engagement scoring is never stored on streams. It is derived from an append-only events table at query time. Every `open`, `pin`, `unpin`, `add`, `confirm_connection` is recorded as an event with a timestamp.

This means the scoring model can change without migrating data. It also means the full history of how you used the tool is preserved — not as a dashboard metric, but as raw material for future analysis.

The decay function is exponential with a 14-day half-life. This is not the only defensible choice, but it reflects a belief that intellectual attention is perishable: what you were reading three weeks ago is not what you are reading now.

### Claude-first, embeddings as fallback

Connection detection calls Claude by default. Embeddings are a fallback for when no API key is configured. This ordering reflects a design choice: semantic similarity is a weak proxy for conceptual connection. Two texts can be close in embedding space without being meaningfully related; two texts can be far apart in embedding space and share a deep structural tension.

Claude is prompted to look for underlying themes, questions, and tensions — not surface topic overlap. The prompt explicitly asks: *even if the surface topics seem different, are these conceptually connected?*

The connection note that Claude returns is written for the user, not for the system. It is not a similarity score or a category label. It is a sentence that explains why two things belong together.

### The pin as a semantic anchor

Pins do not just mark importance. They generate the signal the system uses to curate everything else. The pin note, the pin skills, and the pin embeddings are all inputs to:

- Connection detection (new streams checked against active pins)
- Digest stream selection (pick the stream most relevant to current pins)
- Web discovery (search queries derived from pin skills)

Removing a pin changes what the system surfaces. This is by design. The system is not trying to build a permanent map of your knowledge — it is trying to reflect your current focus back to you in useful ways.

### No delete

There is no delete command. Streams accumulate. Events accumulate. The only removal operation is `unpin`, which timestamps the unpinning but preserves the pin record.

This is partly pragmatic (SQLite is cheap, knowledge is not) and partly philosophical: forgetting is not the same as deleting. Things you stop pinning remain in the lab, fading gradually as their engagement scores decay.

### Channels as clean separation

The channel system enforces a strict boundary between contexts. Streams, pins, connections, and skills are all channel-scoped. The digest runs per-channel. There is no cross-channel recommendation, no global view of your collection.

This is a deliberate rejection of the "second brain" model where everything is connected to everything. Some intellectual projects should not bleed into each other. The channel boundary is the system's way of respecting that.

---

## What the system is not

It is not a read-later app. Adding something to a stream is not a commitment to read it.

It is not a note-taking tool. The system does not expect you to annotate or summarise what you read.

It is not a recommendation engine. The digest is curated for your current focus, not optimised for engagement.

It is not a graph of everything you know. It is a tool for the period of time you are inside a particular question.
