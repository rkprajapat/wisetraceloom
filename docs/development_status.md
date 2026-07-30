# Trailwise SDK — Development Status

Tracks progress feature-by-feature against the plan in [prd.md](prd.md). Update this file whenever a feature moves status — it is the single source of truth for "what's built" going forward.

## How to use this file

- One row per feature. Update **Status** as work progresses; don't add new tracking files elsewhere.
- Move a feature to **Certified** only when its acceptance criteria are met and verified (tests passing, manual check against the criteria noted in the row) — not just "code written."
- When a feature's scope changes, edit its row in place; use the PR/commit that changed it as the history (git blame on this file), not a changelog section here.
- Add new rows if a feature is split or a new one is identified; don't delete rows for abandoned work — mark status **Dropped** with a one-line reason instead.

## Status Legend

| Status | Meaning |
|---|---|
| 🔲 Not Started | No implementation work begun |
| 🚧 In Progress | Actively being built |
| ✅ Built | Implemented, not yet verified against acceptance criteria |
| 🏅 Certified | Implemented **and** verified against its acceptance criteria |
| ⛔ Dropped | Descoped — reason noted |

---

## Stage 1 — MVP / Foundation

**Stage threshold to proceed to Stage 2**: SDK adds <5% latency overhead and never propagates exceptions to the host application.

| # | Feature | Status | Acceptance Criteria | Notes |
|---|---|---|---|---|
| 1.1 | structlog-based capture pipeline with `contextvars` async-safety | 🏅 Certified | Processor pipeline runs sync and async (`await logger.ainfo`); context correctly isolated across concurrent asyncio tasks | PRD §5, §Recommendations Stage 1(a). Implemented in [trailwise/logging.py](../trailwise/logging.py) (`configure`, `get_logger`, `bind_context`); verified by 3 tests in [tests/test_logging_pipeline.py](../tests/test_logging_pipeline.py) — sync logging, async `ainfo` logging, and context isolation across concurrent `asyncio.gather` tasks. All passing (`uv run python -m pytest tests/`). |
| 1.2 | Internal rich schema (agent/tool/loop/eval/cost fields) | 🔲 Not Started | Schema covers agent spans, tool spans, loop-iteration counts, eval scores, cost fields; documented and versioned | PRD §2, §7 (schema versioning) |
| 1.3 | OTel `gen_ai.*` export adapter | 🔲 Not Started | Emits spans/metrics per `open-telemetry/semantic-conventions-genai`; honors `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` | PRD §1, §2 |
| 1.4 | Client-side PII redaction (Presidio, structured + regex first) | 🔲 Not Started | Structured field-name redaction and regex scrubbing active by default; Presidio NER layer available; no raw PII in emitted logs for test fixtures | PRD §3 |
| 1.5 | Fail-open wrapper around all instrumentation | 🔲 Not Started | Any exception inside SDK instrumentation is caught; host application logic unaffected; verified via fault-injection tests | PRD §5, §7 |
| 1.6 | Prompt fingerprinting + auto version registration | 🔲 Not Started | SHA-256 hash over normalized prompt template; new hash auto-registers a version; repeat hash links to existing version; default system-generated titles | PRD §8.1, §8.2 |
| 1.7 | Decorators / context managers for instrumentation (`@trace`, `with trailwise.span()`) | 🔲 Not Started | One-line integration for common call sites (LLM call, tool call, agent step) | PRD §5 |
| 1.8 | W3C Trace Context propagation | 🔲 Not Started | `traceparent`/`tracestate` correctly propagated across process boundaries; single propagation format used throughout | PRD §5 |
| 1.9 | Latency overhead & fail-open threshold check | 🔲 Not Started | Benchmark shows <5% added latency; chaos test confirms zero propagated exceptions to host | Stage 1 exit gate — must certify before Stage 2 work is certified |
| 1.10 | Log destination & rotation configuration store (SQLModel/SQLite) | 🏅 Certified | Log file path and rotation settings (size threshold, time interval, backup count, compression) persisted in SQLite via SQLModel; size and time rotation triggers are independently configurable and combinable (rotates on whichever fires first); `configure()` in the capture pipeline resolves the log destination (explicit `file_path` arg → stored `log_file_path` → stdout) and, when a file is resolved, applies the stored rotation config to a real rotating file handler | PRD §5 "Log file management" (rotation, compaction, retention), extends feature 1.1. Schema in [trailwise/config.py](../trailwise/config.py) (`RotationConfig`, `get_rotation_config`, `set_rotation_config`) carries a nullable `tenant_id` column now so a per-tenant configuration manager can be layered on later without a migration — today lookups fall back tenant-specific → global default → built-in default. Handler factory in [trailwise/rotation.py](../trailwise/rotation.py) (`build_rotating_handler`, `SizeAndTimeRotatingFileHandler`). Verified by 13 tests across [tests/test_config.py](../tests/test_config.py) (incl. `log_file_path` round-trip), [tests/test_rotation.py](../tests/test_rotation.py) (handler selection, functional size rollover, forced time rollover, gzip compression), and [tests/test_logging_file_output.py](../tests/test_logging_file_output.py) (explicit `file_path`, fallback to stored `log_file_path`, explicit arg overriding stored config). All passing (18/18 full suite). Config domain intentionally scoped to log destination + rotation for this pass — level/format and other domains (PII, sampling, OTel export) remain out of scope until their owning features start. |

---

## Stage 2 — Enterprise Hardening

**Stage threshold to proceed to Stage 3**: Pass SOC 2 controls; demonstrate GDPR Art. 17 erasure with an intact audit chain.

| # | Feature | Status | Acceptance Criteria | Notes |
|---|---|---|---|---|
| 2.1 | Delta-Lake-inspired append-only storage (versioned JSON commits + Parquet checkpoints + OCC) | 🔲 Not Started | Writers retry on version conflict; checkpoints compact every ~10 commits; time-travel by version/timestamp works | PRD §1, §5 |
| 2.2 | Crypto-shredding for erasure | 🔲 Not Started | Per-subject key encryption; key deletion renders ciphertext unrecoverable; two-phase "Requested"→"Confirmed" erasure workflow | PRD §3, §7 |
| 2.3 | Tamper-evident hash-chained audit log with externally anchored Merkle roots | 🔲 Not Started | Each entry embeds hash of previous; root anchored outside operator's control; broken-chain detection test passes | PRD §7 |
| 2.4 | Per-tenant cost attribution + quota kill-switches | 🔲 Not Started | Cost tagged at request-creation time in harness layer; per-tenant daily spend cap enforced; alert on spend vs 7-day rolling baseline | PRD §7 |
| 2.5 | Data-residency routing (India-region for RBI/DPDP) | 🔲 Not Started | Regulated-data writes routed to configured region (e.g., ap-south-1); routing rule covered by test | PRD §3 |
| 2.6 | Server-side fail-closed masking callback | 🔲 Not Started | Masking failure blocks storage of the unmasked event (fail-closed), distinct from fail-open instrumentation behavior | PRD §3, §7 |
| 2.7 | Human-titleable prompt versions + promotion aliases | 🔲 Not Started | Owner can set semantic title via API/UI; `production`/`canary`/`shadow` aliases point at specific versions without redeploy | PRD §8.2 |
| 2.8 | Automated eval-on-detection with regression gating | 🔲 Not Started | New prompt version auto-triggers golden-set eval; promotion to `production` blocked if thresholds regressed (pass rate, cost, p95 latency) | PRD §8.3 |
| 2.9 | Multi-tenancy & isolation (RBAC, per-tenant namespaces) | 🔲 Not Started | Tenant data isolated at storage and query layer; RBAC enforced on viewer access | PRD §7 |
| 2.10 | SOC 2 / GDPR Art. 17 certification gate | 🔲 Not Started | SOC 2 control checklist passes; erasure demonstrated end-to-end with audit chain intact post-erasure | Stage 2 exit gate |

---

## Stage 3 — Scale & UX

**Stage threshold**: Sustained high-volume production ingest with a highly available tail-sampling Collector.

| # | Feature | Status | Acceptance Criteria | Notes |
|---|---|---|---|---|
| 3.1 | Embedded spin-up viewer (MLflow/Jaeger model) | 🔲 Not Started | `trailwise ui` / `trailwise serve` launches local web server against configured backend store | PRD §6 |
| 3.2 | Multi-step trace view with per-step cost breakdown | 🔲 Not Started | Single request's LLM/tool calls shown in one trace view with cost per step | PRD §6 |
| 3.3 | Prompt-version comparison scorecard | 🔲 Not Started | Side-by-side A/B / champion-challenger view across titled versions on same dataset | PRD §8.3, §6 |
| 3.4 | Hybrid head + tail sampling via Collector | 🔲 Not Started | Head-based pre-filter plus tail-based error/latency rules; trace-ID-based load balancing keeps a trace on one Collector | PRD §7 |
| 3.5 | SIEM exporters (Splunk / ELK / Datadog) | 🔲 Not Started | Export adapter validated against at least one target SIEM in a test environment | PRD §7 |
| 3.6 | Anomaly detection + alerting (incl. prompt-version drift) | 🔲 Not Started | Alerts fire on eval-score drift, cost spikes, loop detection, and prompt-version regression in production | PRD §7, §8.3 |
| 3.7 | Real-time streaming/tailing (WebSocket/SSE) | 🔲 Not Started | Live tail shows incremental trace assembly as late spans arrive | PRD §6 |
| 3.8 | Production ingest scale test | 🔲 Not Started | Sustained load test at target volume with HA tail-sampling Collector; no data loss under Collector failover | Stage 3 exit gate |

---

## Summary

| Stage | Total Features | Certified | In Progress | Not Started |
|---|---|---|---|---|
| Stage 1 | 10 | 2 | 0 | 8 |
| Stage 2 | 10 | 0 | 0 | 10 |
| Stage 3 | 8 | 0 | 0 | 8 |

*Update the Summary table whenever a row's Status column changes.*
