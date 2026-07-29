# Changelog

Newest first. Each entry names the commit, what changed, why, and how to verify
it. Spotter is built in strict verified steps — every entry below shipped only
after its step's acceptance test passed.

---

## 2026-07-29 — Dashboard fixes

### Focus-zone priority ladder + adaptive layout

**What:** Two dashboard bugs.
- `_pick_hero` only examined the single highest-priority active project, so
  the focus zone showed the empty state whenever that project had no live
  task even though lower-priority active projects did. It now walks active
  projects in priority order and spotlights the first with a candidate task
  (per-project candidacy unchanged: the tool's own is_next-else-oldest-open
  rule). The empty state appears only when no active project has any live
  task. Paused/done projects are never considered.
- The fixed two-column split left a mostly empty right half when job
  applications and triggers were sparse. The column split is now decided
  server-side: with 3 or fewer right-column items (applications + triggers),
  the page renders single-column and the work list takes the full width.
  General density pass: tighter heading margins, hero padding, row padding,
  and section gaps.

**Verify:** with the top project's tasks all done/waiting, the hero shows the
next project's task; with ≤3 applications+triggers the page is single-column
at desktop width.

---

## 2026-07-29 — Correctness & operations batch

### `d53679d` — Surface waiting tasks in briefs; plain-text Telegram output; drop dead doc command

**What:** Three hygiene fixes, no reasoning changes.
- The morning brief and evening check-in previously filtered tasks to
  `open`/`in_progress`, so anything marked `waiting` vanished from both. The
  shared task context (`brief._format_active_tasks`) now includes waiting
  tasks rendered as `WAITING (6d)` — days since the status change, taken from
  `updated_at`. `paused` remains deliberately excluded.
- Telegram sends use no `parse_mode`, so any markdown the model wrote
  (`**bold**`, `## headers`) rendered as literal asterisks and hashes. Fixed
  at two layers: the brief/check-in and trigger-generation system prompts now
  forbid markdown outright, and `brief.to_plain_text()` sanitizes all three
  generated outbound paths (morning brief, evening check-in, prompt-type
  reminders) at compose time — paired `**`/`__` and leading `#` headers are
  stripped; single `*`/`_` (bullets, snake_case, math) are left alone. The
  brief is sanitized before persisting so `daily_briefs` matches what was sent.
- Removed `commands.doc` from `prompts.yaml` — it referenced the deferred
  `update_workspace_doc` tool and could only misfire.

**Verify:** tonight's check-in has no literal `**`; a waiting task shows in
tomorrow's brief with its age; `/doc` no longer appears in the command list.

### `d62aeac` — Weekly SQLite backups with retention and boot catch-up

**What:** The entire database lived as one file on one Railway volume with no
copy. `src/backup.py` snapshots it using **sqlite3's online backup API**
(`Connection.backup`) — a consistent point-in-time copy even while the live
process is writing, which a raw file copy cannot guarantee. Backups land on
the same volume at `<db_dir>/backups/spotter-YYYYMMDD-HHMMSS.db` (UTC stamps,
chronologically sortable names) and prune to the newest `BACKUP_RETAIN`
(default 4).

**Scheduling:** Sunday 03:00 local via the existing APScheduler wrapper. The
cron is in-memory and resets on every redeploy, so boot also checks
`backup_is_due()` — newest backup missing or older than 7 days — and runs a
catch-up in the background (the same pattern as the morning-brief catch-up).
Manual one-shot: `python -m src.backup`.

**Verify:** `python -m src.backup` prints a path; the directory never exceeds
4 files; deleting `data/backups/` and rebooting logs a catch-up backup.

### `6820c48` — Separate local dev bot token

**What:** Local runs shared the production bot token, so a local process and
Railway fought over Telegram `getUpdates` (recurring 409s). When
`TELEGRAM_DEV_BOT_TOKEN` is set, it wins for that process — a local run polls
a separate BotFather bot and cannot collide with production.
`TELEGRAM_DEV_ALLOWED_USER_ID` optionally overrides the allowlist, and is
ignored unless the dev token is active, so it can never leak into a
production config. Boot logs `Using DEV bot …` (warning level) or
`Using PRODUCTION bot`. `TELEGRAM_BOT_TOKEN` stays required either way.
Local-only: never set the dev vars on Railway.

**Verify:** with the dev token in `.env`, boot logs the DEV warning and the
dev bot answers against the local DB while Railway keeps answering the
production bot — no 409s in either log.

### `97edefc` — Insert-only seeding (fixes seed clobbering)

**What:** The highest-priority correctness fix. Every boot re-imposed the
seed snapshot on the live database:
- The next-action upsert matched on `(project_id, is_next=1)`. Completing a
  task clears `is_next`, so the next boot found no match and **re-inserted the
  completed task as a new open row** — completed tasks resurrected on every
  deploy.
- Facts had every field rewritten from the seed each boot; project `status`
  was reset to the seeded value (a chat-paused project un-paused itself on
  deploy).

`seed_context` now bootstraps, never enforces:
- **Tasks** — keyed by `(project, title)` case-insensitively across ALL
  statuses; an existing row in any state is skipped entirely. New tasks get
  `is_next=1` only when no live next action exists.
- **Projects** — inserts carry seeded values; existing rows only accept
  `priority`/`description` (seed-managed metadata). `status` and the goal
  layer (`goal`, `current_bottleneck`, `goal_updated_at`) are never touched.
- **Facts** — insert if missing; update from the seed only while pristine
  (`updated_at == created_at`). A runtime-modified fact wins permanently.
- `SeedResult` now logs inserted/updated/skipped per entity, so a healthy
  boot on a live DB reads `tasks_inserted=0, tasks_skipped=4`.

**Verify:** mark a task done, redeploy, it stays done; pause a project,
redeploy, it stays paused.

---

## 2026-07-28/29 — Goal layer (5 steps)

### `d5313cf` — Progress awareness (step 5)

Completing a task on a project with an active milestone now triggers
evaluation: in chat, the `update_task_status` tool result carries an
instruction to judge whether the milestone is finished (marking it done
auto-activates the next); from the dashboard, completion kicks off a logged
background brain turn with the same ask, while the toast shows only the
confirmation line. The shared brief system prompt (morning + evening) gains a
per-project goals block — goal, active milestone, bottleneck — with framing to
reason about progress toward goals rather than task counts. Dashboard project
headers show a compact `▸ milestone · goal · bottleneck` line.

### `23be9f1` — `prepare_handoff` + dashboard "Get the prompt" (step 4)

Spotter holds strategy; Claude Code executes. The 14th tool assembles a full
handoff context (objective, stack, goal, bottleneck, milestone position,
related and completed work, blockers with resolution ideas, core workspace
facts) and instructs the model to compose a ready-to-paste Claude Code prompt:
Objective / Context / Constraints / Definition of done, in one fenced code
block. Targets a task or milestone by id/title; defaults to the active
milestone (else next task). The dashboard focus zone gained a **Get the
prompt** button (`/api/handoff`, unlogged turn, fences stripped server-side)
with copy-to-clipboard.

### `0e7c260` — Goal-aware `surface_next_action` (step 3)

With a goal set, the tool returns reasoning context — goal, bottleneck, active
milestone plus upcoming ladder, open tasks, recent completions, open
blockers — and instructs the model to derive ONE concrete step that advances
the active milestone and to say why in those terms. No-goal projects keep the
original stored-task behavior plus a prompt to set a goal. `smaller_than`
shrink preserved on both paths.

### `5f2cd95` — Goal-layer tools (step 2)

Four new tools in the standard handler pattern: `set_project_goal`,
`decompose_goal`, `update_milestone`, `set_bottleneck`. `decompose_goal` is
two-phase (context out → model reasons → milestones in), the same convention
as `smaller_than`. `update_milestone` enforces at-most-one-active per project
and auto-activates the next pending on done. Schema entries appended to
`tools_schema.json` (additions only).

### `da70a88` — Goal schema (step 1)

`projects` gains nullable `goal`, `current_bottleneck`, `goal_updated_at`
(1:1 state → columns, not a side table), ALTERed onto existing DBs via the
guarded migration pattern. New `milestones` table (`project_id`, `title`,
`description`, `pending/active/done/dropped`, `order_index`), created by
`create_all` like `job_applications`. Nothing backfilled.

---

## 2026-07-27/28 — Web dashboard (4 steps + agent wiring)

### `300c6c7` — Agent-wired focus zone

The hero shows a Brain-generated concrete next step (same tool loop as
Telegram) instead of the stored task title, cached per project keyed to the
exact task row (10-min TTL). Buttons: shrink (`smaller_than`), regenerate,
"Am I stalling?" (logged turn). Completing the hero invalidates the cache so
the next load auto-generates the following action. Tasks untouched 3+ days
get age markers; a stale `is_next` task is flagged. `Brain.respond` gained
`log=False` for ephemeral dashboard turns. Page loads make zero API calls on
a warm cache.

### `517cfeb` — Dashboard UI overhaul

Focus zone, thin status strip, two-column layout (>900px), tasks grouped
under compact project headers, collapsed quick-add and forms
(`<details>`/`<summary>`, no JS), auto-submitting status dropdowns (the only
JS besides the hero fetches).

### `95c4575` — Quick-add + job applications (web step 3)

`job_applications` table + tracker section (pipeline summary, status
dropdowns), quick-add forms for tasks and captures (captures via the
`capture_item` tool with a new internal `source` field → `'dashboard'`),
recent-captures section.

### `fe36107` — Button actions (web step 2)

POST `/tasks/status` runs the existing `update_task_status` tool handler in
the same ToolContext/transaction shape the brain uses — web and chat share one
write path. POST `/stalls/resolve` flips the resolved flag. Both behind the
auth middleware; Post/Redirect/Get with the tool's own confirmation as toast.

### `0a6a1a3` — Password-gated dashboard (web step 1)

aiohttp on the same asyncio event loop as PTB/APScheduler, started from
`post_init` — one process, one DB. Read-only view (projects, tasks, stalls,
pending triggers). `DASHBOARD_PASSWORD` gate: unset means the server never
binds. HMAC session cookie (HttpOnly, SameSite=Lax, Secure behind TLS),
1-second failed-login delay. All DB access via `asyncio.to_thread` with
short-lived sessions — the pattern the bot already used.
