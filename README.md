# Spotter

**A single-user AI agent that externalizes executive function — built to interrupt the exact points where projects stall.**

Spotter is a personal accountability agent that runs on Telegram with a companion web dashboard. Instead of a passive to-do app you have to maintain, it's an agent you talk to in plain language: it captures what you tell it, tracks what you're working on and what each project is *for*, surfaces the concrete next action when a task feels too big, and names it directly when you're avoiding the finish line. It runs 24/7, sends a morning brief and an evening check-in, fires reminders you set in plain language, and otherwise waits until you come to it.

Built with Python, the Anthropic API as the reasoning core, SQLite for layered memory, and deployed on Railway as a single process — bot, scheduler, and dashboard on one asyncio event loop.

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

### The tools

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
| `update_task_status` | Mark tasks/projects done, paused, waiting, reopened |
| `schedule_reminder` | Turn "remind me at 6" into a scheduled one-shot or recurring trigger |
| `set_project_goal` | Record a project's target state in plain language |
| `decompose_goal` | Two-phase: read goal + current state, then write ordered milestones |
| `update_milestone` | Advance the milestone ladder; one active per project, auto-advances on done |
| `set_bottleneck` | Record the single most-blocking thing on a project |
| `update_workspace_doc` | (Deferred) optional Google Doc mirror |

### The goal layer

Projects aren't flat task lists: each can carry a **goal** (the target state in plain language), a **current bottleneck** (the one thing most in the way), and an ordered ladder of **milestones** between the current state and the goal. `decompose_goal` is the reasoning step — the model reads the project's goal, open tasks, blockers, and recent activity, then writes the milestones that actually stand between here and there. At most one milestone is active per project; completing it auto-activates the next. This is what lets "what should I work on?" be derived from where the project is headed instead of a priority lookup.

### Proactive messaging

Beyond the daily morning brief and evening check-in (both assembled fresh from workspace state at fire time), `scheduled_triggers` rows drive one-shot reminders and recurring check-ins created from chat. The firing loop catches up after downtime — a missed trigger fires once on boot, then a recurring one resumes its normal schedule across DST boundaries.

### Layered memory

Memory is tiered so context stays lean and cost stays controlled:

- **Working memory** — the last ~20 conversation turns, passed every call.
- **Core context** — a small set of always-injected facts (role, patterns, priorities), flagged `is_core` in the database.
- **Long-term memory** — the rest of the facts, *not* injected by default; reachable on demand via `query_memory` (FTS5). This keeps the system prompt small while keeping everything retrievable.

Full-text search runs over SQLite FTS5 virtual tables — keyword retrieval, no vector store. At single-user scale, semantic search would add a network hop and an embedding pipeline to maintain for recall the keyword index handles fine. The storage layer is the right place to swap that in *if* data volume ever justifies it — a contained change, not a rewrite.

### Stall detection

Stall detection is layered across three places rather than hardcoded: behavioral instructions in the system prompt (the agent notices patterns conversationally), the dedicated `name_the_stall` tool (which logs a structured event and dedupes against recent identical stalls), and a `/stall` command for explicit checks. The dashboard adds passive detection: any task untouched for 3+ days shows its age, and a stale "next" task is flagged as a possible stall.

### The web dashboard

A password-gated dashboard served **from the same process** — same event loop as the bot and scheduler, same SQLite database, no second app. Server-rendered HTML over aiohttp; the only JavaScript is dropdown auto-submit and the agent-backed focus zone.

- **Focus zone** — not the stored task title, but a concrete next step *generated by the agent* (the same `Brain` + tool loop as Telegram), cached per project and invalidated when the task changes. Buttons: **Done** (auto-advances to the next action), **Too big — shrink it** (repeatable, via `surface_next_action`'s `smaller_than`), regenerate, and **Am I stalling?** (a real logged agent turn that can record a stall).
- **State at a glance** — status strip, tasks grouped by project priority, stalls, job-applications pipeline, upcoming triggers, recent captures.
- **Write paths shared with chat** — dashboard buttons call the same tool handlers the model dispatches (`update_task_status`, `capture_item`), so web and Telegram can never disagree about what a write means.
- **Auth** — a single shared secret (`DASHBOARD_PASSWORD`); if it's unset the server never binds rather than running open. HMAC session cookie (HttpOnly, SameSite=Lax, Secure behind TLS), 1-second delay on failed logins.

A typical page load makes **zero** model API calls — agent output is cached and embedded server-side; a cache miss generates once in the background while the page stays usable.

---

## Stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| Interface | Telegram (`python-telegram-bot`, long polling, single-user allowlist) + aiohttp dashboard |
| Reasoning | Anthropic API (`claude-sonnet-4-6`) |
| Storage | SQLite (SQLAlchemy 2.x) on a persistent volume |
| Search | SQLite FTS5 (keyword, no vectors) |
| Scheduler | APScheduler (in-process: morning brief, evening check-in, reminders) |
| Config | YAML prompts, env-based secrets, seed context via file or env var |
| Hosting | Railway (single always-on process; dashboard on the injected `PORT`) |

---

## Running it

```bash
# local
python -m venv .venv && .venv/Scripts/activate   # or source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                              # fill in tokens + DASHBOARD_PASSWORD
cp seed/context.example.yaml seed/context.yaml    # fill in your projects/facts
python -m src.main                                # bot + scheduler + dashboard on :8080
```

Schema setup and migrations run automatically on every boot and are idempotent — new tables arrive via `create_all`, new columns via guarded `ALTER`s; nothing is ever dropped.

**Deploy (Railway):** push to `main` (auto-deploy), with the service configured per `railway.toml` — all `.env.example` variables set, a volume at `/data` with `DB_PATH=/data/spotter.db`, a generated public domain for the dashboard, and exactly one instance. `PORT` is injected by Railway.

| Env var | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID` | Bot identity + single-user allowlist |
| `ANTHROPIC_API_KEY`, `DEFAULT_MODEL` | Reasoning core |
| `DB_PATH` | SQLite location (volume path in prod) |
| `BRIEF_TIME`, `EVENING_TIME`, `TIMEZONE` | Daily brief / check-in schedule |
| `SEED_CONTEXT_YAML` | Seed context in prod (file is gitignored) |
| `DASHBOARD_PASSWORD` | Dashboard gate — unset disables the web server entirely |
| `PORT` | Dashboard port (Railway injects; local default 8080) |
| `BACKUP_RETAIN` | Weekly DB backups to keep (default 4) |
| `TELEGRAM_DEV_BOT_TOKEN` | **Local only** — separate dev bot so local runs never 409 against production |
| `TELEGRAM_DEV_ALLOWED_USER_ID` | Local only, optional — allowlist override while the dev bot is active |
| `GROQ_API_KEY` | Optional, unused for now |

Change history with per-commit detail lives in [CHANGELOG.md](CHANGELOG.md).

---

## Notable engineering decisions

**Config seeding that separates public code from private context.** The user's personal context lives in a gitignored `seed/context.yaml` locally, and in a private environment variable (`SEED_CONTEXT_YAML`) in production — never in the repo. A sanitized `context.example.yaml` ships publicly so anyone can configure their own. The seed loader resolves file → env var → clear error, so the same code runs locally and in the cloud without exposing personal data.

**Idempotent, key-based seeding.** Seed facts carry a stable `key`; re-seeding upserts on that key rather than matching on content, so editing a fact's wording never creates a duplicate. Safe to re-run on every boot.

**Reliability guards built in, not bolted on.** A hard iteration cap on the tool loop prevents cost runaway; an API error is caught and answered with a graceful message rather than crashing the bot; the scheduler's morning brief upserts on a unique date so a re-run never violates the constraint; the SQLite database lives on a persistent volume so memory survives redeploys.

**Seeding bootstraps; it never enforces a snapshot.** Seed data is insert-only for anything carrying live state: tasks are keyed by (project, title) and skipped once they exist in any status (a completed task can't be resurrected by a redeploy), project status and the goal layer are never rewritten, and a fact modified at runtime permanently beats its seed version. Boot logs report exactly what the seed inserted vs. skipped.

**Backups on a schedule, plus catch-up.** A weekly job snapshots the database with SQLite's online backup API (consistent even mid-write) to the same volume, pruning to the last `BACKUP_RETAIN`. Because the in-memory cron resets on every redeploy, boot also backs up whenever the newest copy is missing or older than a week — the same catch-up pattern the morning brief uses. Manual: `python -m src.backup`.

**Dev/prod bot separation.** A local run with `TELEGRAM_DEV_BOT_TOKEN` set polls a separate BotFather bot, so it can never steal `getUpdates` from the deployed poller; boot logs which bot is active.

**Plain-text outbound formatting.** Telegram renders markdown literally without a `parse_mode`, so every generated outbound message (brief, check-in, reminders) is sanitized at compose time — and the prompts forbid markdown in the first place.

**An anti-recursion guardrail.** The system prompt explicitly instructs the agent to refuse helping build *new Spotter features* while a higher-priority project sits unfinished — because that "productive procrastination" is the exact pattern Spotter exists to interrupt. The tool is designed to resist its own scope creep.

---

## How it was built

Spotter was built in strict, verified steps, each committed only after its acceptance criteria passed — a deliberate structure, since the project's own subject matter is a documented tendency to stall near completion. The sequence: skeleton → Telegram echo → database → model integration → tools (implemented in waves, proving the loop on two tools before scaling to eight) → context seeding → morning brief → deploy. The commit history reflects that progression. Two separate pre-publication passes caught and removed sensitive data — a seeded test credential and a personal context file — from git history *before* the repo went public, using key rotation and history rewriting respectively.

---

## Status & roadmap

**Live** — deployed on Railway, running 24/7, in personal daily use.

**Working:** the full agent loop with 18 tools; the memory layer — GitHub webhook + Claude Code session-note ingestion into a provenance event log (`occurred_at` vs `recorded_at`, per-source confidence, supersession), semantic retrieval with explainable hybrid re-ranking (Voyage embeddings fused with recency decay, source confidence, and subject match; keyword fallback), and searchable conversation history; the conditions engine (at most one data-driven nudge per day); voice input via Groq Whisper; session start/stop warm-starts; the goal/milestone layer with goal-aware next actions and progress-aware briefs; Claude Code handoff prompts; the agent-backed web dashboard with embedded chat and a job-applications tracker; insert-only seeding; weekly backups with boot catch-up; dev/prod bot separation; and the reliability guards above.

**Roadmap (deliberately deferred, not missing):**
- Commitment-date nudges (needs parsed dates, not free-text intents)
- The optional Google Doc workspace mirror
- User identity as setup config rather than prompt text

---

*Spotter is a single-user personal agent. It is deployed and in active daily use.*
