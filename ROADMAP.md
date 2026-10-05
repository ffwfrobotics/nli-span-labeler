# E13 labeler roadmap

Work items for building the E13 labelling app (`docs/e13/E13_LABELING_APP_REQUIREMENTS.md`)
out of the NLI span labeler. The legacy code was evaluated in `docs/e13/E13_CODEBASE_EVALUATION.md`.
Its findings appear below as tasks, tagged **[eval #n]**.

Legend: `[x]` done · `[~]` partly done (the note says what's missing) · `[ ]` to do.
Each FR's acceptance test is defined in the requirements doc. A task is done when that test exists and passes.

---

## M0: hollow out the legacy app

- [x] Merge the requirements and workspace-file branches; evaluate the requirements against the code.
- [~] Tag the pre-fork state `legacy-final` (commit `72c9bbb`). The tag exists locally, but this session can't push tags, so the owner needs to push it.
- [x] Move `app.py` and `static/` into the `e13_labeler` package as a pure rename, so `git log --follow` keeps the lineage.
- [x] Strip premise/hypothesis features: tokenizer, Tier 0/1 auto-spans, sliders, NLI labels, pools, calibration, training mode, leaderboard, MCP server, download script.
- [x] Keep and adapt the backbone: FastAPI app, rate limiter, CORS, request logging, sessions, locks, flags, base CSS, auth/help/tab JS.
- [x] Schema per requirements §5.1, with WAL and `user_version` migrations.
- [x] `pyproject.toml` + `uv.lock`. Dependencies upgraded; transformers, datasets, nltk and mcp dropped; CI runs through uv.
- [x] CLI: `python -m e13_labeler init-db | create-owner | serve`.

## M1: MVP, owner only

### Import (§4.1)
- [x] **FR-1** Import E09 pool JSONL (`importer.py`). The real libre sample (`docs/e13/fixtures/pool_eval_libre_sample.jsonl`, 384 rows → 695 items) imports with 0 rejects; `tests/test_import_pool.py` covers it, including "first 100 rows".
- [x] **FR-2** One item per (row, qid); `item_id = "<row id>#<qid>"`. Row ids containing `#` are rejected.
- [~] **FR-3** Render `choice` / `score` / `noul` questions per `docs/e13/fixtures/README.md`: noul default texts, `_` → space for slug keys, sentence keys selectable, qid fallback, pretty JSON. Done in `label.js`; still needed: an automated render test per case.
- [x] **FR-4** Detect the state format (`json` when the state is, or parses to, an object or array); `state_format` overrides.
- [~] **FR-5** Store the state byte-exact, plus `state_sha256`. Still needed: the export → re-import round-trip test (comes with FR-45). A state given as an object is serialised once and that string is stored.
- [x] **FR-6** Tier resolution: explicit field, then source class; unverified/unknown → restricted; Jev raises.
- [~] **FR-7** Optional `e13.*` fields, stored per item (`candidate_for` split by qid). Still needed: the "never sent to labelers" test (with FR-12).
- [x] **FR-8** `model_answers` stored per item, hidden; a Jev teacher raises the tier (showing answers is FR-34).
- [x] **FR-10** Idempotent import (same hash = no-op, different hash = reject unless `--replace`); `import_runs` and the audit log are written.
- [~] **FR-11** `python -m e13_labeler import FILE --batch NAME` is done. Still needed: the admin upload page (M2).
- [x] **FR-55** Refuse eval-only sources: the seed list plus `E13_EVAL_ONLY_SOURCES`. Neither seed source is in `source_permissions.json` **[eval #8]**. The source keys must be checked against the real pool data.
- [x] **FR-56** Max-tier rule. A replacement that lowers a tier needs `--allow-lower-tier`, which is audit-logged.

### Labelling (§4.2, §6.1, §6.2)
- [x] **FR-12** Blind `GET /api/next` payload (§5.3): no `gold`, `source`, `e13`, `model_answers` or `is_gold_probe`, and no gold-based filters **[eval #1]**.
- [x] **FR-13** Ten independent reason toggles; `answerable` is exclusive; reject empty submissions. The API and UI are done.
- [x] **FR-14** Note (≤ 2,000 chars), required per batch config (`batches.require_note`). The API and UI are done.
- [x] **FR-15** Span roles support/refute/unsupported/framing, with side validation. The API and UI are done.
- [x] **FR-16** Spans link to an option and to checked reasons. The API and UI are done.
- [x] **FR-17** Coordinates: char offsets, RFC 6901 pointer + offsets, option-side offsets; `text == slice`. Validated server-side; produced by the UI.
- [~] **FR-18** Word-snapped selection, Alt for character precision, plus the keyboard path (←/→, Shift extends). Still needed: highlight the exact characters of a char-precise span; the whole touched words light up now. Offsets are code points (as Python slices), not JS UTF-16 units; checked with emoji and `\r\n` in Chromium.
- [x] **FR-19** Span policy per reason; block submit; `Shift+Enter` override, recorded.
- [x] **FR-20** Skip with a reason code (`x` then `1`…`5`); never re-served to that labeler.
- [~] **FR-22** Active time (visible tab + interaction in the last 60 s) and wall time from lock hand-out. Both are done; still needed: an automated "hidden tab for 5 min adds < 1 s" browser test.
- [x] §6.1 labelling screen and §6.2 keyboard map (`static/label.js`). It fits 1366×768; checked in Chromium with `tests/e2e/label_smoke.js`. All 695 real sample items render with no JS errors and no horizontal scroll.
- [x] Span rendering: colour **and** underline style per role (§6.1, NFR-9).

### Agreement and export
- [ ] **FR-9** Import model pseudo-labelers (§5.4 JSONL, `labeler_kind: "model"`).
- [~] **FR-37** Per-reason Krippendorff's α (nominal, binary), with n, pairable values, prevalence and a 95% bootstrap CI. Done in `agreement.py`; still needed: wiring to the DB (blind batches only, gold excluded).
- [x] **FR-38** (first bullet) α for "any abstain" (`agreement.any_abstain_units`).
- [x] **FR-40** One shared α module (`e13_labeler/agreement.py`, stdlib only), tested against the `krippendorff` package and Krippendorff's 2011 worked example.
- [ ] Simple dashboard table (§6.4 subset).
- [ ] **FR-45** Annotation export (§5.4) and its JSON Schema.
- [ ] **FR-46** Training export (§5.5).
- [ ] **FR-48** Agreement export, reproducible offline.
- [ ] **FR-49** Exports under `outputs/e13_labeler/exports/<timestamp>/` with a manifest.
- [ ] **FR-29** Gold CRUD (API shape from the old `/api/admin/gold*`).
- [x] **FR-54** `SINGLE_USER=1`: owner auto-login, loopback only, including when forwarded headers are spoofed **[eval #5]**.

### Owner re-label
- [ ] Owner-vs-owner α: re-label after a gap as a second labeler `L01-r2` (§10 M1).

## M2: multi-labeler

### Access control (§4.8, §8)
- [~] **FR-50 / FR-57** Server-side clearance filter. Done for `next` (with the batch `tier_ceiling`), submit, skip, locks and flags. Still needed: history, quiz, adjudication and export, plus a fuzz test over every item endpoint.
- [ ] **FR-51** Invites: single use, 7-day expiry, bound to a role and clearance; stored hashed. The `invites` table exists.
- [~] **FR-52** Labeler management. Done: list and `revoke_sessions`. Still needed: pause, resume, revoke endpoint, clearance change (owner only), password reset, per-labeler stats.
- [~] **FR-53** Audit log. Done: table, `create_owner`, `login`. Still needed: clearance changes, exports, imports, adjudications, gold edits.
- [ ] **FR-58** Quiz and gold for `public` labelers must be libre; warn when there are fewer than 12 libre gold items.
- [ ] **FR-60** Contributor agreement, versioned, accepted at first login.
- [ ] **FR-59** `permissions`, `source_license` and `text_included` on every export row.

### Security (NFR-5)
- [x] argon2id passwords (was salted SHA-256 with a non-constant-time compare).
- [x] Session tokens stored hashed (were plaintext).
- [x] Cookies `HttpOnly` + `SameSite=Strict`; `Secure` via `COOKIE_SECURE=1`.
- [x] Login rate limiting that can't be bypassed: forwarded headers are trusted only from `TRUSTED_PROXIES` **[eval #2]**.
- [ ] CSRF tokens on mutating requests **[eval #3]**.
- [ ] HTTPS deployment for external labelers (§11 Q5).
- [x] No hard-coded credentials: the owner comes from the CLI; the MCP default password is gone with `mcp_server/` **[eval #4]**.

### Onboarding and QA (§4.4)
- [ ] **FR-26** Versioned guideline page, gate before the quiz.
- [ ] **FR-27** Quiz with immediate feedback, a pass rule and one retake. The old training mode completed after 5 items whatever the accuracy; reuse its overlay CSS.
- [ ] **FR-28** Hidden gold at 0.20 for the first 50 items, then 0.05; excluded from α and from training export.
- [ ] **FR-30** Rolling gold accuracy; auto-pause and a retraining quiz.

### Batches and queue (§4.5)
- [~] **FR-31** Batches with `overlap_target ≥ 2`, `tier_ceiling`, span policy and status. Done: schema, import `--batch`, open/close (CLI + API). Still needed: create/edit batch settings.
- [x] **FR-32** Overlap-aware `next`: complete pairs first, then priority, then random; the property test with 5 labelers × 200 items passes. The old routing was breadth-first, the opposite **[eval, §2]**.
- [ ] **FR-35** Progress and ETA.
- [ ] **FR-21** Edit the last 20 submissions; versioned annotations.

### Agreement, adjudication (§4.6)
- [ ] **FR-39** Span token-F1/Jaccard per role and per reason; E07 AP with ≥ 3 labelers.
- [ ] **FR-41** Full dashboard: candidates vs the min/median of established reasons, confusion matrix, per-labeler gold accuracy.
- [~] **FR-42** "α unstable" warning (< 30 positives or < 3% prevalence). Computed in `agreement.describe`; still needed: show it on the dashboard.
- [ ] **FR-43** Adjudication queue, anonymised L-a/L-b, stored separately from raw labels.
- [~] **FR-44** Flags. Done: API, admin list and the `f` key. Still needed: resolving flags.
- [ ] §7.3 monitoring: flag median active time < 5 s and reason prevalence above 3× the batch rate.

### Operations
- [~] **NFR-6** WAL is on. Still needed: nightly online backup to `outputs/e13_labeler/backups/`; a no-hard-delete policy in code.
- [~] **NFR-7** The app version and git commit are recorded per annotation. Still needed: the DB checksum in the export manifest.
- [x] **NFR-10** One `run.sh`, configured by env vars. It binds 127.0.0.1 unless `HOST` is set (the old script bound `0.0.0.0` with `--reload`).

## M3: extensions

- [ ] **FR-23 / FR-24** Relation task type and consistency warnings.
- [ ] **FR-25** Threshold stance for score questions (off by default).
- [ ] **FR-33** `reason_set` per batch; `null` for reasons not asked.
- [ ] **FR-34** `show_model_answer` audit batches, reported separately.
- [ ] **FR-36** State-run serving with `position_in_state_run`.
- [ ] **FR-38** MASI set-valued α; **FR-39** unitized α_U (COULD).

## Cross-cutting

- [x] **NFR-3** No CDN or build step. Tested.
- [~] **NFR-8** Tests run offline **[eval #6]**. Still needed: tests for every MUST FR, a blindness test, a tier fuzz test and the assignment property test. The α reference test is done.
- [x] **NFR-4** Pseudonyms (`L01`, …); contact details only in the owner-only `identity` table.
- [~] **NFR-1** `next`/submit p95 < 200 ms with 50k items and 10 labelers. Schema v3 indexes `annotations(item_id, …)`; a full queue pass over the 695-item sample went from 49 s to 13 s. Still needed: a 50k-item benchmark, and replacing `ORDER BY RANDOM()` full scans.
- [ ] **NFR-2** Answerable item in one keystroke plus Enter; median ≤ 20 s (measure in the pilot).
- [ ] **NFR-9** Visible focus everywhere; roles distinguishable without colour.
- [x] `.gitignore` covers `outputs/` and the DB files. Test fixtures can be committed (the blanket `*.jsonl` ignore is gone) **[eval #10]**.
- [x] Reproducible environment: `uv.lock` replaces the stale `.uv-freeze.txt` **[eval #7]**.

## Schema drift to watch (from `docs/e13/fixtures/README.md`, "Hedging")

- [ ] The row schema isn't frozen (A2.1 / D8). Relation, reason and evidence fields may move, so re-check §5.4/§5.5 before building the exports.
- [ ] The canonical JSON form for dict/list criteria isn't settled (FR-3). The UI pretty-prints with 2-space indentation.
- [ ] The noul default descriptions come from E09 student code, not a contract. They are one constant in `label.js`.

## Waiting on the owner (requirements §11)

- [ ] Q3: final reason list (`out_of_scope`?). It fixes the key map.
- [ ] Q7: the reference time for `stale_state`. It decides whether `e13.asof` is shown to labelers.
- [ ] Q2: the keep rule; Q8: `false_premise` scope; Q9: the boundary order for ambiguous/underspecified/subjective; Q10: overlap of 2 or 3.
- [x] Import sample: `feature/e13-test-fixtures`, merged with its JSONL (sha256 matches the README).
- [ ] Which teachers count as Jev (FR-8)? Currently `jev*` plus `E13_JEV_TEACHERS`. A Jev answer to any question raises **every** item of that row, since the output came from the shared state. Confirm that this conservative reading is wanted.
