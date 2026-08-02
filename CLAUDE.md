# Spotter — agent instructions

Spotter is a single-user personal AI agent: Telegram bot + web dashboard +
memory layer, one Python 3.12 process on Railway. Read `README.md` for
architecture and `CHANGELOG.md` for per-commit history before large changes.

Ground rules for working in this repo:

- Built in strict verified steps: implement the requested step, run it, stop
  for verification. Commit per step with a detailed message.
- `prompts.yaml` and `schema.sql` are validated artifacts — read them, don't
  modify without explicit approval. `tools_schema.json` accepts additions only.
- Reuse existing patterns: tool handlers (`src/tools/base.py`), guarded
  additive migrations (`src/db/database.py`), `asyncio.to_thread` for DB work
  on the event loop, refuse-rather-than-run-open for secrets.
- Production DB lives on the Railway volume (`DB_PATH=/data/spotter.db`,
  absolute — boot refuses relative paths on Railway). Railway's Redeploy
  button rebuilds the SAME commit; verify the active deployment's commit hash
  after pushing.

## Spotter session notes
At the END of every working session — when the user says they're done, wrapping
up, or switching projects — report the session to Spotter:

curl -s -X POST "$SPOTTER_URL/webhooks/session" \
  -H "X-Spotter-Secret: $SPOTTER_SESSION_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"project": "Spotter", "worked_on": "...", "shipped": "...", "blocked": "...", "next": "..."}'

One or two sentences per field; omit fields that don't apply. If the env vars
are missing, skip silently — never ask about it.
