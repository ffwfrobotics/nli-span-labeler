# E13 requirements vs. the pre-fork codebase

**Date:** 2026-10-05
**Evaluated tree:** `72c9bbb` (the last GoblinCorps merge), plus the merged `feature/e13-labeler-requirements` and `feature/untracked-workspace-files` branches.
**Against:** `docs/e13/E13_LABELING_APP_REQUIREMENTS.md` (draft, 2026-10-05).

This is a pre-fork snapshot. After it, the old app can be changed or replaced freely.

---

## 1. Summary

- **The requirements doc audited an older copy of this repo.** Its §3 describes `/storage/nli-span-labeler` at 42 commits (a 5,362-line `app.py`, a 5,175-line `index.html`). That matches commit `4c394bb`. This repo has **17 more commits** (14 non-merge, +2,753 lines) on top. The extra work is: bulk import endpoints, an MCP server for agent-driven labelling, `slowapi` rate limiting, CORS lockdown, request logging, edge-case flags, a rubric help modal, and slider keybindings. None of it changes the §3 conclusions. Some of it is worth reusing (§4). Every line reference in the doc's §3 is now out of date. §2 below lists the current ones.
- **The §3.4 gap analysis holds.** I checked each row against the code (§2). Two findings make the blindness and security gaps worse than the doc says (§3).
- **The §3.5 recommendation still holds,** with the owner's location override: build E13 as a new package in this repo and don't retrofit the premise/hypothesis tables. Every MUST in §4 of the requirements is new work. None can be met by adapting existing endpoints. What carries over are patterns, plus about 300 lines of infrastructure (locks, rate limiting, middleware, the keyboard skeleton).
- **Baseline health:** all 61 existing tests pass (`pytest tests/`, Python 3.11). They need network access to download the ModernBERT tokenizer from the HF Hub. This conflicts with NFR-8 ("tests run without the GPU or network") for anything that keeps that dependency.

---

## 2. Verification of the doc's §3.4 gap table (current line numbers)

| E13 needs | doc's claim | verified in this tree | status |
|---|---|---|---|
| state text/JSON + typed question | `examples(premise, hypothesis)` | `app.py:981` `examples` table; `ExampleResponse` and all UI are premise/hypothesis | confirmed, schema rewrite |
| 10 reasons + `answerable` + note | sliders + NLI labels | `get_label_schema()` `app.py:293`: 6 difficulty dims, 3 NLI relations, custom labels; edge-case flags + rationale (`0e781f9`) are the nearest analogue | confirmed, new |
| char offsets / JSON pointers, roles, option-side spans | word indices from tokenizer | `tokenize_text()` `app.py:533` does keep char offsets per token (`return_offsets_mapping`), but spans are stored as word indices on premise/hypothesis. UI is click-to-toggle words (no `getSelection` anywhere in `static/index.html`), so there is no drag-select or Alt char-precision path | confirmed, new span model and new selection UI |
| blind labelling | `/api/next` returns gold; UI shows it | `/api/next` returns `gold_label`/`gold_label_text` (`app.py:4876`); UI renders it (`static/index.html:3443-3448`, `5113`). **Also:** `/api/next?gold_label=...` filters by gold (`app.py:4767`, `4821`), so a labeler can request items by their answer | confirmed, worse than stated |
| per-reason Krippendorff's α | 1−MAD/5, pairwise Jaccard | `calculate_complexity_agreement` `app.py:1439`, `calculate_span_agreement` `app.py:1474`; no α, no CI, no `krippendorff` dependency | confirmed, new |
| permission tiers / clearance | none | no tier column, no clearance, no source→licence mapping | confirmed, new |
| never twice, overlap targets | exclusion keyed on `complexity_scores`/`skipped` | `app.py:4790-4801`. A submission without complexity scores leaves the item eligible again. Routing order is `zero_entry → building → test` (`app.py:4829-4851`), which is **breadth-first: the opposite of FR-32's "complete pairs first"** | confirmed, rewrite |
| strong hashing | salted SHA-256 | `hash_password`/`verify_password` `app.py:1999-2011`; comparison is `==` (not constant-time) | confirmed, upgrade |
| time per item | none | no `visibilitychange` or timing code in the UI, no timing columns | confirmed, new |
| (cookie flags) | `samesite=lax`, no `secure` | `app.py:2436-2441`, `2478-2483`: `httponly=True`, `samesite="lax"`, no `secure` | confirmed |

Other §3 claims checked:
- **Calibration routing:** `get_user_calibration_status` `app.py:1955`, `should_serve_test_question` `app.py:1981`. Env defaults are 0.8 and 0.1, as stated.
- **Training/gold flow:** `/api/training/*` `app.py:2995-3260`; `gold_annotations` `app.py:1138`. Training **completes after `TRAINING_MIN_EXAMPLES` (5) submissions whatever the accuracy**. Accuracy only seeds a reliability score. There is no pass/fail and no retake, unlike FR-27.
- **Locks:** `acquire_lock`/`release_lock` `app.py:1871-1925`. Check-then-insert is not atomic under concurrent connections. Fine for SQLite with a single writer, but the E13 version should use `INSERT ... ON CONFLICT` with a `WHERE until < now` guard.
- **Keyboard skeleton:** `handleKeyDown` `static/index.html:2810-2830+`, with three more ad hoc `keydown` listeners at `4082`, `5028` and `5563`.
- **Storage:** `get_db()` `app.py:1423` opens a new connection per request, with `foreign_keys=ON` and **no WAL** (NFR-6).

---

## 3. Findings not in the requirements doc

| # | finding | where | bears on |
|---|---|---|---|
| 1 | Gold-label **filter** on `/api/next` (labelers can choose items by answer). | `app.py:4767` | FR-12 blindness |
| 2 | Rate-limit key trusts `X-Forwarded-For`/`X-Real-IP` from any client, so login rate limiting is bypassable by setting the header. | `get_real_ip` `app.py:146` | NFR-5 |
| 3 | Session tokens are stored in plaintext (`sessions.token`). No CSRF protection anywhere. | `app.py:947`, `2014` | NFR-5 |
| 4 | MCP server ships a default credential (`NLI_API_PASSWORD` defaults to `mcp-annotator-password`). | `mcp_server/server.py:23` | NFR-4/5 |
| 5 | `run.sh` binds `0.0.0.0` with `--reload`, including in `ANONYMOUS_MODE`. | `run.sh:12` | FR-54 (loopback only) |
| 6 | Tests download the HF tokenizer and `nltk` WordNet at run time. | `app.py:222` (WordNet download), `app.py:523` (tokenizer load) | NFR-8 |
| 7 | `.uv-freeze.txt` predates `slowapi`, `mcp` and `nltk`. The freeze does not reproduce the current `requirements.txt`. | `.uv-freeze.txt` | NFR-7 |
| 8 | `source_permissions.json` has 101 sources: 48 `libre`, 15 `restricted`, 38 `unverified`. So **53 of 101 resolve to `restricted`** under FR-6. `llm_aggrefact` and `halubench` are **not in it**. Without the FR-55 config list they would import as `restricted` instead of being refused. | `docs/e13/source_permissions.json` | FR-6, FR-55 |
| 9 | No external CDN. The page is self-contained, which already satisfies NFR-3's "no CDN at runtime". | `static/index.html` | NFR-3 (keep) |
| 10 | `.gitignore` ignores `*.jsonl` and `*.db` globally. E13 import fixtures and test JSONL need an exception (as `scripts/*.jsonl` has). | `.gitignore` | NFR-8 fixtures |

---

## 4. Reuse inventory

| component | where | verdict for E13 |
|---|---|---|
| Lock acquire/release/extend + `/api/lock/*` | `app.py:1871-1953`, `1986`, `4278-4416` | **Adapt.** Make it atomic; 20 min default (FR-32). |
| `slowapi` limiter | `app.py:146-175`, `425-427` | **Reuse** for login rate limiting (NFR-5), after fixing finding #2 (only trust forwarded headers from a configured proxy). |
| CORS lockdown + request logging middleware | `app.py:437-518` | **Reuse** as is. Logging is a start toward the FR-53 audit log, but the audit log must be a DB table. |
| Calibration routing (hidden test rate by user status) | `app.py:1955-1985` | **Idea only.** FR-28 needs "first 50 items at 0.20, then 0.05", gold excluded from α. |
| Training/gold flow | `app.py:2800-3260` | **Idea only.** The data model (complexity scores) doesn't transfer, and FR-27 needs pass/fail, a per-reason miss rule and one retake. |
| Flag → admin list | `/api/flag`, `/api/admin/flagged*` `app.py:3805-4017` | **Adapt** for FR-44 (kinds: `bad_item`, `guideline_unclear`). |
| Bulk import endpoints | `/api/import`, `/api/import/jsonl` `app.py:4111-4277` | **Idea only.** FR-10/FR-11 need hash idempotency, `import_runs` and a CLI. |
| Keyboard handler skeleton | `static/index.html:2810+` | **Idea only.** §6.2's map is denser and modal (`x` then `1..5`, `s` then `a..z`); write a small key state machine. |
| MCP server | `mcp_server/` | **Optional, later.** It could front E13 for Claude-assisted pilot labelling. Any such labels must be recorded as `labeler_kind: "model"`, never human (FR-9, α validity). |
| Tier 0/1 auto-spans, tokenizer, difficulty sliders, NLI labels, leaderboard, consensus pools, export | most of the rest | **Drop.** Not in E13 scope. The tokenizer and WordNet dependencies are what make the tests need network access. |

---

## 5. Requirement coverage by milestone

The legend: **new** means nothing usable exists; **adapt** means an existing pattern or code carries over with changes; **reuse** means it carries over largely as is.

**M1 (owner only)**

| FRs | topic | status |
|---|---|---|
| FR-1 – FR-7, FR-10, FR-11 | pool import, item = (state, qid), question rendering, state format, sha256, tier resolution, hidden `e13` fields, idempotency, CLI | new |
| FR-55, FR-56 | eval-only refusal list, max-tier rule | new (note finding #8) |
| FR-12 – FR-14 | blind payload, reason toggles with exclusive `answerable`, note | new (remove gold display, finding #1) |
| FR-15 – FR-18 | four span roles, option-side spans, char offsets + RFC 6901 pointers, word-snap with Alt override | new (UI selection model is a rewrite) |
| FR-19, FR-20 | span policy with override, skip codes | new (skip table exists but has no codes) |
| FR-22 | active/wall time | new |
| FR-9 | model pseudo-labeler import | new |
| FR-37, FR-38 (any-abstain), FR-40 | α with bootstrap CI, shared module tested against `krippendorff` | new |
| FR-45, FR-46, FR-48, FR-49 | annotation / training / agreement exports + manifest | new (current export is premise/hypothesis JSONL/CSV) |
| FR-29 | gold CRUD | adapt (`/api/admin/gold*` CRUD shape) |
| FR-54 | `SINGLE_USER`, loopback only | adapt (`ANONYMOUS_MODE`, but must refuse non-loopback, finding #5) |

**M2 (multi-labeler)**

| FRs | topic | status |
|---|---|---|
| FR-50 – FR-53, FR-57, FR-58, FR-60 | clearance enforcement, invites, labeler management, audit log, contributor agreement | new (role/admin-user listing adapts) |
| NFR-5 | argon2id, Strict cookies, CSRF, hashed sessions/invites, rate-limited login | adapt (limiter reuse; everything else new) |
| FR-26 – FR-28, FR-30 | guideline gate, quiz with pass rule, hidden gold rates, auto-pause | adapt (training/calibration ideas) |
| FR-31, FR-32, FR-35 | batches, overlap-aware "complete pairs first" queue, progress/ETA | new (the current routing order is the opposite) |
| FR-21 | edit last 20, versioning | new |
| FR-39, FR-41, FR-42 | span agreement, dashboard, low-prevalence warning | new |
| FR-43, FR-44 | adjudication, flags | adapt (flag flow) |
| NFR-6 | WAL + nightly backup | new |

**M3:** FR-23 – FR-25, FR-33, FR-34, FR-36 and MASI/α_U are all new.

---

## 6. Recommendations for the clean break

1. **Build `e13_labeler/` as a fresh package in this repo** (`python -m e13_labeler ...`), with its own `run.sh`, tests and SQLite file. Don't import from `app.py`. Copy in the few pieces listed as reuse/adapt in §4.
2. **Remove the legacy app from the working tree** once E13 M1 boots: `app.py`, `static/index.html`, `mcp_server/`, `scripts/download_data.py` and the legacy tests. Git history keeps them; a `legacy-final` tag on `72c9bbb` makes the pre-fork state easy to find. This also drops `transformers`, `datasets` and `nltk`, so tests no longer need the network (NFR-8).
3. **Decide what happens to `CLAUDE.md`.** Its GoblinCorps PR roles (Frick/Frack/Contraption) describe a workflow the requirements doc calls "a different team workflow". Replace it with E13 dev notes, or keep it if that team will build E13.
4. **Fix before anything is exposed to external labelers:** findings #1–#5 are inherent in the old code. They disappear with the rewrite, but the new code must not re-introduce them (trusted-proxy handling, CSRF, hashed tokens, loopback bind).
5. **Before M1 starts, ask the owner for an import sample** of `pool_eval.jsonl` restricted to `libre` sources, to use as test fixtures for FR-1 – FR-5. Also ask for the `eval_only_no_training` source keys as they appear in the pool data (finding #8).
6. **Open questions** in the requirements doc §11 that block M1 design: Q3 (the final reason list, which sets the key map) and Q7 (the `stale_state` reference time, which decides whether `e13.asof` is shown). Q1 is already resolved by the owner's fork decision.
