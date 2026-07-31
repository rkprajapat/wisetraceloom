# Examples

`quickstart.py` — runnable, end-to-end tour of wisetraceloom's public API:
`configure`, nested `agent_step`/`tool_call`/`llm_call`, the
`trace_tool_call` decorator, `bind_context`, rotation config, OTel export
opt-in, prompt versioning, and cross-process trace propagation. Uses fake
`search`/`call_llm` stand-ins, so it needs no network access or API keys.

`customer_support_agent.py` — a closer-to-production scenario: a support
triage agent processes a batch of tickets (customer lookup -> intent
classification -> refund). Shows PII redaction firing automatically on both
a structured field (`email`/`phone`) and free text embedded with an email
address, a tool call that raises a real exception (and how that's distinct
from wisetraceloom's own fail-open guarantee), and per-ticket multi-tenant
context via `bind_context`.

Run either from the repo root (as a module, so `wisetraceloom/` resolves off the
repo root rather than the script's own directory):

```bash
uv run python -m example.quickstart
uv run python -m example.customer_support_agent
```

**Note:** rotation config (`set_rotation_config`) is persisted in SQLite —
once any script sets a `log_file_path`, every later `wisetraceloom.configure()`
call across *any* script sharing that database (the default is
`.wisetraceloom/wisetraceloom.db` under the repo root) resolves to that file
instead of the console, until it's changed again. Both examples create
`logs/` up front so that's never a crash, only an expected consequence of
persisted config — see `wisetraceloom.set_db_path` in the README if you want an
example's config isolated from the others.
