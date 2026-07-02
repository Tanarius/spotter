# Spotter

**A single-user AI agent that externalizes executive function — built to interrupt the exact points where projects stall.**

Spotter is a personal accountability agent that runs on Telegram. Instead of a passive to-do app you have to maintain, it's an agent you talk to in plain language: it captures what you tell it, tracks what you're working on, surfaces the concrete next action when a task feels too big, and names it directly when you're avoiding the finish line. It runs 24/7, sends one proactive morning brief, and otherwise waits until you come to it.

Built with Python, the Anthropic API as the reasoning core, SQLite for layered memory, and deployed on Railway.

---

## Why it exists

Standard productivity tools fail for executive-function-limited users for one structural reason: **they require maintenance** — manual task entry, status updates, inbox triage — which *is* the executive-function load they're supposed to relieve. The tool that demands upkeep gets abandoned in about two weeks.

Spotter is built on a different principle: **delegation, not reminders.** It does the work and presents a result for approval, rather than nagging you to do it yourself. It's narrowed to three specific failure points:

1. **Task initiation** — when a task feels too big to start, Spotter shrinks it to the literal next physical action ("open this file, type these three words"), removing the ambiguity that causes the freeze.
2. **The 70–80% completion stall** — it notices when a near-done project is being avoided in favor of new ideas, redesigns, or scope creep, and names the stall directly instead of letting the avoidance slide.
3. **Executive-function buffer** — when you mention needing to reply to someone or schedule something, it drafts it for review rather than just reminding you it exists.

---

## Architecture

Spotter is a tool-using agent with a transparent loop. The reasoning model is the router — there is no separate intent classifier.

```
Telegram message
  → allowlist check (single-user)
  → build request: system prompt + injected core context + recent history + tool schema
  → Anthropic API call
  → if stop_reason == "tool_use":
        dispatch tool → read/write SQLite → append result → re-call model
        (loop, hard-capped at 10 iterations)
  → if stop_reason == "end_turn":
        reply to Telegram, log the turn (role, content, token counts)
```

**Why the model routes instead of a classifier:** a separate intent classifier would mean a second model to tune and a coordination problem when the two disagree. A well-described tool schema lets one model decide competently. The trade-off (tool schema in context every turn) is negligible at single-user scale, and the reduction in moving parts is meaningful.

### The 8 tools

Each tool maps to a specific job, not a generic capability:

| Tool | Job |
|---|---|
| `capture_item` | Save a thought/link/follow-up; the agent categorizes, the user doesn't decide |
| `surface_next_action` | Return the next concrete step on a project; shrinkable if still too big |
| `name_the_stall` | Call out 70–80% avoidance directly; logs the event |
| `log_blocker` | Record "stuck on X because Y"; feeds stall detection |
| `query_memory` | FTS5 search across captured items, tasks, blockers, and facts |
| `draft_message` | Draft an email/Slack/text for approval — never sends |
| `schedule_intent` | Record a scheduling intent (no calendar write in V1) |
| `update_workspace_doc` | (Deferred) optional Google Doc mirror |

### Layered memory

Memory is tiered so context stays lean and cost stays controlled:

- **Working memory** — the last ~20 conversation turns, passed every call.
- **Core context** — a small set of always-injected facts (role, patterns, priorities), flagged `is_core` in the database.
- **Long-term memory** — the rest of the facts, *not* injected by default; reachable on demand via `query_memory` (FTS5). This keeps the system prompt small while keeping everything retrievable.

Full-text search runs over SQLite FTS5 virtual tables — keyword retrieval, no vector store. At single-user scale, semantic search would add a network hop and an embedding pipeline to maintain for recall the keyword index handles fine. The storage layer is the right place to swap that in *if* data volume ever justifies it — a contained change, not a rewrite.

### Stall detection

Stall detection is layered across three places rather than hardcoded: behavioral instructions in the system prompt (the agent notices patterns conversationally), the dedicated `name_the_stall` tool (which logs a structured event and dedupes against recent identical stalls), and a `/stall` command for explicit checks.

---

## Stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| Interface | Telegram (`python-telegram-bot`, long polling, single-user allowlist) |
| Reasoning | Anthropic API (`claude-sonnet-4-6`) |
| Storage | SQLite (SQLAlchemy 2.x) on a persistent volume |
| Search | SQLite FTS5 (keyword, no vectors) |
| Scheduler | APScheduler (in-process, one morning-brief job) |
| Config | YAML prompts, env-based secrets, seed context via file or env var |
| Hosting | Railway (single always-on worker) |

---

## Notable engineering decisions

**Config seeding that separates public code from private context.** The user's personal context lives in a gitignored `seed/context.yaml` locally, and in a private environment variable (`SEED_CONTEXT_YAML`) in production — never in the repo. A sanitized `context.example.yaml` ships publicly so anyone can configure their own. The seed loader resolves file → env var → clear error, so the same code runs locally and in the cloud without exposing personal data.

**Idempotent, key-based seeding.** Seed facts carry a stable `key`; re-seeding upserts on that key rather than matching on content, so editing a fact's wording never creates a duplicate. Safe to re-run on every boot.

**Reliability guards built in, not bolted on.** A hard iteration cap on the tool loop prevents cost runaway; an API error is caught and answered with a graceful message rather than crashing the bot; the scheduler's morning brief upserts on a unique date so a re-run never violates the constraint; the SQLite database lives on a persistent volume so memory survives redeploys.

**An anti-recursion guardrail.** The system prompt explicitly instructs the agent to refuse helping build *new Spotter features* while a higher-priority project sits unfinished — because that "productive procrastination" is the exact pattern Spotter exists to interrupt. The tool is designed to resist its own scope creep.

---

## How it was built

Spotter was built in strict, verified steps, each committed only after its acceptance criteria passed — a deliberate structure, since the project's own subject matter is a documented tendency to stall near completion. The sequence: skeleton → Telegram echo → database → model integration → tools (implemented in waves, proving the loop on two tools before scaling to eight) → context seeding → morning brief → deploy. The commit history reflects that progression. Two separate pre-publication passes caught and removed sensitive data — a seeded test credential and a personal context file — from git history *before* the repo went public, using key rotation and history rewriting respectively.

---

## Status & roadmap

**Live** — deployed on Railway, running 24/7, in personal daily use.

**Working:** the full agent loop, 7 of 8 tools, layered memory with on-demand retrieval, the scheduled morning brief, idempotent seeding, and the reliability guards above.

**Roadmap (deliberately deferred, not missing):**
- Commitment-anchored check-ins (proactive nudges tied to stated deadlines, not clock-based)
- Voice input (Whisper transcription)
- The optional Google Doc workspace mirror
- User identity as setup config rather than prompt text
- An eval harness for prompt-regression testing at scale

---

*Spotter is a single-user personal agent. It is deployed and in active daily use.*
