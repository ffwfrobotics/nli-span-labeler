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
- [x] **FR-5** States stored byte-exact with `state_sha256`. Export (`items.jsonl`, pool format) → re-import gives byte-identical states, hashes and item columns, on the synthetic and real pool samples (`tests/test_exports.py::TestRoundTrip`).
- [x] **FR-6** Tier resolution: explicit field, then source class; unverified/unknown → restricted; Jev raises the release tier only. Visibility (who may see an item) comes from the text's licence.
- [x] **FR-7** Optional `e13.*` fields, stored per item (`candidate_for` split by qid), never sent to labelers (`test_payload_shape`; `asof` is the one exception, shown for every item).
- [x] **FR-8** `model_answers` stored per item, hidden; a Jev teacher raises the tier (showing answers is FR-34).
- [x] **FR-10** Idempotent import (same hash = no-op, different hash = reject unless `--replace`); `import_runs` and the audit log are written.
- [x] **FR-11** `python -m e13_labeler import FILE --batch NAME`, and Admin → Batches → Import (`POST /api/admin/import`, same importer; lowering a tier stays CLI-only).
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
- [x] **FR-9** Model pseudo-labelers: `python -m e13_labeler import-labels FILE` (§5.4 rows, `labeler_kind: "model"`). Spans are validated, imports are idempotent, and Jev models raise the row's release tier.
- [x] **FR-37** Per-reason α (nominal, binary) from the DB: human, blind batches, gold probes and re-labels excluded, with n, pairable values, prevalence, positives and a 95% bootstrap CI. Matches `krippendorff` on DB data (`analysis.py`, `records.py`).
- [x] **FR-38** Any-abstain α; human vs each model and model vs model per reason; intra-rater α from re-label batches, reported separately. MASI is M3.
- [x] **FR-40** One shared α module, with `unit_key` for intra-rater units (`by_item_and_labeler`) so re-label α is never pooled with inter-rater α, (`e13_labeler/agreement.py`, stdlib only), tested against the `krippendorff` package and Krippendorff's 2011 worked example.
- [x] Dashboard table (Admin tab, §6.4 subset): inter-rater α with CI, prevalence and positives; candidates vs the min/median of established reasons; "α unstable"; intra-rater; human/model sections; batch list; an Export button. CLI: `python -m e13_labeler agreement`.
- [x] **FR-45** Annotation export (§5.4), latest version per (item, labeler, batch), pseudonyms only. Validates against `e13_labeler/schemas/annotation.schema.json`. Includes `asof` and `app_version`.
- [x] **FR-46** Training export (§5.5): soft targets (null for reasons not asked), majority with ties left null (plus `tied_reasons`), spans merged with votes, adjudicated (when present), `text_included` mode. Human, blind, first-pass labels only; gold excluded by default. `train.schema.json`.
- [x] **FR-47** Export filters: batch, exact `permissions`, date range, models in or out, gold in or out, and the exporter's clearance (CLI flags and the API body).
- [x] **FR-48** Agreement export: every number plus the exact records, so `analysis.report(doc["data"])` reproduces it (tested).
- [x] **FR-49** Exports go under `outputs/e13_labeler/exports/<timestamp>/`, never overwritten, with a manifest: app version, guideline versions, DB snapshot sha256, filters, per-file sha256 and rows. Audited.
- [x] **FR-29** Gold create/edit/retire/promote, with explanation and acceptable alternatives, validated like labels (including the hard span rules). CLI: `gold list|import|promote|retire`; API: `/api/admin/gold`; in the UI, through adjudication ("Save + promote to gold"). A standalone gold editor page would still help (M3).
- [x] **FR-54** `SINGLE_USER=1`: owner auto-login, loopback only, including when forwarded headers are spoofed **[eval #5]**.

### Owner re-label
- [x] Owner-vs-owner α: re-label batches serve the owner's own items again, blind, after the gap. Intra-rater α shows as its own dashboard and CLI section, never pooled.

## M2: multi-labeler

Built 2026-10-05. Exit criteria (§10) still to meet in practice: two or more
labelers finish one double-labelled batch, and per-reason α with CI is exported.

### Access control (§4.8, §8)
- [x] **FR-50 / FR-57** Server-side clearance filter on every item route: `next` (with the batch `tier_ceiling`), submit, skip, edit and history (with the batch's Jev filter, review §3.5), locks, flags, gold, quiz, adjudication, agreement and exports. A fuzz test covers every `/api` route.
- [x] **FR-51** Invites: single use, 7-day expiry, bound to a role and clearance, stored hashed; only the owner invites admins or internal clearance. Registering without a valid token is 403.
- [x] **FR-52** Labeler management: list with stats, pause, resume (back to onboarding unless a quiz was passed; the owner may skip it), revoke (ends sessions at once), clearance change (owner only), single-use reset links, `/api/admin/labelers/{p}/stats`.
- [x] **FR-53** Audit log: logins, registrations, invites, status and clearance changes, resets, agreement acceptance, quiz results, imports, exports, backups, gold edits, adjudications, flag resolutions. Append-only (a trigger refuses updates and deletes).
- [x] **FR-58** Quiz and hidden gold follow clearance, so `public` labelers get libre gold only; opening a batch warns when public labelers have fewer than 12 libre gold items.
- [x] **FR-60** Contributor agreement (`contributor.py`), versioned, accepted before anything else; the owner is exempt. The text is a draft until §11 Q4 (licence) is settled.
- [x] **FR-59** `permissions`, `source_license`, `text_included` and `label_provenance: "human"` on every training row; `permissions` on every annotation row.

### Security (NFR-5)
- [x] argon2id passwords (was salted SHA-256 with a non-constant-time compare).
- [x] Session tokens stored hashed (were plaintext).
- [x] Cookies `HttpOnly` + `SameSite=Strict`; `Secure` via `COOKIE_SECURE=1`.
- [x] Login rate limiting that can't be bypassed: forwarded headers are trusted only from `TRUSTED_PROXIES` **[eval #2]**.
- [x] CSRF tokens on mutating requests **[eval #3]**: `X-CSRF-Token` must match a token derived from the session (a per-process one in SINGLE_USER mode), read by JS from the `e13_csrf` cookie.
- [~] HTTPS deployment for external labelers (§11 Q5): `deploy/Containerfile`, `deploy/compose.yaml` (Caddy, automatic certificates, fixed proxy address for `TRUSTED_PROXIES`), `docs/e13/DEPLOY.md`. Still needed: build the image and run the checklist once; no container runtime was available here.
- [x] No hard-coded credentials: the owner comes from the CLI; the MCP default password is gone with `mcp_server/` **[eval #4]**.

### Onboarding and QA (§4.4)
- [x] **FR-26** Versioned guideline page (`guideline.json`, a draft v1 with a positive and a near-miss example per reason; definitions and span rules come from the code). Opening it is recorded and gates the quiz; annotations record the version in force. `g` opens it while labelling.
- [x] **FR-27** Quiz: 12 gold items covering every reason plus 2 answerable, feedback after each answer, pass at 75% (exact or Jaccard ≥ 0.8, alternatives honoured) with no established reason missed twice; one retake after re-reading the guideline, then the owner reviews.
- [x] **FR-28** Hidden gold at 0.20 for the first 50 items, then 0.05, through an ordinary batch; never repeated or taken from the labeler's quiz; excluded from α and from training export. Owners get no probes.
- [x] **FR-30** Rolling 30-probe gold accuracy since the last passed quiz; under 0.6 (after 10+ probes) pauses the account and `next` offers the retraining quiz.

### Batches and queue (§4.5)
- [x] **FR-31** Batches: `overlap_target` 1..n (default 3, owner Q10), `tier_ceiling`, span policy, status, priority, note rule, a deterministic reliability subset (`reliability_fraction` × `reliability_overlap`) and re-label batches (`relabel_of`, `relabel_after_days`). Configurable via `python -m e13_labeler batch config|relabel` and `/api/admin/batches/{name}/config|relabel`. Opening a batch warns what its α will rest on.
- [x] **FR-32** Overlap-aware `next`: complete pairs first, then priority, then random; per-item targets for the reliability subset; re-label batches as the one exception to "never twice". The property test (5 labelers) passes for overlap 1, 2 and 3. The old routing was breadth-first, the opposite **[eval, §2]**.
- [x] **FR-35** Progress and ETA: items at 0/1/2/3+ labels, % complete, labels remaining, ETA from the 24 h (else 7 d) pace; per labeler today/total/median active time.
- [x] **FR-21** Edit the last 20 submissions (`e`), each edit a new version, until the batch closes; α and exports use the latest.

### Agreement, adjudication (§4.6)
- [x] **FR-39** Span token-F1/Jaccard (word units) per role and per triggering reason; leave-one-out AP with ≥ 3 labelers (`spans_agreement.py`). To do: check the AP against E07's own implementation, which isn't in this repo.
- [x] **FR-41** Dashboard: candidates vs the min/median of established reasons, the reason confusion matrix between labeler pairs (plus co-occurrence), per-labeler gold accuracy (overall, per reason, rolling) and pairwise agreement. All of it in the agreement export, reproducible from its `data`.
- [x] **FR-42** "α unstable" warning (< 30 positives or < 3% prevalence), on the dashboard and in the CLI.
- [x] **FR-43** Adjudication queue (most disagreements first), labels side by side as L-a/L-b in an item-specific order, internal admins only, versioned results stored apart from raw labels, "Save + promote to gold"; the training export's `adjudicated` carries the latest.
- [x] **FR-44** Flags: API, admin list, the `f` key, and resolve/dismiss with a note.
- [x] §7.3 monitoring: median active time < 5 s and reason prevalence above 3× the batch rate are flagged on the dashboard and in the CLI.

### Operations
- [x] **NFR-6** WAL; online backups (`backup.py`: at start-up when the newest is over 24 h old, then hourly checks; `python -m e13_labeler backup`; owner `POST /api/admin/backup`), integrity-checked and never deleted; triggers refuse deletes of items, labelers, labels, spans, gold, adjudications, flags, quiz data and the audit log.
- [x] **NFR-7** The app version and git commit are recorded per annotation; the export manifest records the DB checksum.
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
- [~] **NFR-8** Tests run offline **[eval #6]**: the blindness test, the tier fuzz test, the assignment property test and the α reference test are in, and every MUST FR's stated test has a pytest except the browser-only ones (FR-3 render, FR-18 drag, FR-22 hidden tab), which have Playwright smoke scripts in `tests/e2e/` but no automated harness yet.
- [x] **NFR-4** Pseudonyms (`L01`, …); contact details only in the owner-only `identity` table.
- [~] **NFR-1** `next`/submit p95 < 200 ms with 50k items and 10 labelers. Schema v3 indexes `annotations(item_id, …)`; a full queue pass over the 695-item sample went from 49 s to 13 s. Still needed: a 50k-item benchmark, and replacing `ORDER BY RANDOM()` full scans.
- [ ] **NFR-2** Answerable item in one keystroke plus Enter; median ≤ 20 s (measure in the pilot).
- [ ] **NFR-9** Visible focus everywhere; roles distinguishable without colour.
- [x] `.gitignore` covers `outputs/` and the DB files. Test fixtures can be committed (the blanket `*.jsonl` ignore is gone) **[eval #10]**.
- [x] Reproducible environment: `uv.lock` replaces the stale `.uv-freeze.txt` **[eval #7]**.

## Review of b18fbb6 (2026-10-05)

Fixed in this round:
- [x] §3.1 `conflicting_evidence` (support + refute on one option) and `stale_state` (the dated phrase) are hard span rules: no batch policy or Shift+Enter relaxes them (`reasons.HARD_SPAN_RULES`).
- [x] §3.2 Visibility is restricted if the source class **or** the row's explicit tier is restricted, so `"permissions": "libre"` can't expose restricted text.
- [x] §3.3 A re-import with an unchanged state still raises the tier when Jev output is newly attached (`n_raised`, audited `raise_tier`).
- [x] §3.4 Each annotation stores the as-of date it was shown (schema v6: `annotations.asof`, carried on the lock).
- [x] §3.6 Requirements body: FR-19 (`false_premise` against the state only; the hard rules) and FR-31 (minimum 1, default 3) updated.
- [x] §3.7 Labelers no longer see who holds a lock or the batch's real name (`batch <id>`); admins still do.
- [x] §5 A Host allowlist (`ALLOWED_HOSTS`; loopback names only in SINGLE_USER) blocks DNS rebinding.
- [x] §5 Cookies are `Secure` by default outside SINGLE_USER (`COOKIE_SECURE=0` to opt out, with a warning).
- [x] §5 `serve` turns off uvicorn's proxy headers; `TRUSTED_PROXIES` is the only proxy trust.
- [x] §5 Only active accounts may label, skip, flag or extend locks.
- [x] §5 `data/` is ignored again.
- [x] §8 A tier fuzz test covers every `/api` route with a public session (FR-57).

Still open from the review:
- [~] §3.5 The `show_model_answer` Jev exception is enforced in `next`, submit, history and edit (via `_batch_filter`). Exports and the agreement report filter by the exporter's clearance only; give them the same Jev filter when FR-34 (`show_model_answer` batches, M3) lands.
- [x] §2 Finish M1: FR-29, FR-9, FR-37 DB wiring, FR-45/46/48/49 and the dashboard table are all done.
- [~] §8 The FR-5 round trip is done. Still needed: FR-3 render tests (needs a browser test harness).
- [x] §5 CSRF tokens (M2).

## Schema drift to watch (from `docs/e13/fixtures/README.md`, "Hedging")

- [ ] The row schema isn't frozen (A2.1 / D8). Relation, reason and evidence fields may move, so re-check §5.4/§5.5 before building the exports.
- [ ] The canonical JSON form for dict/list criteria isn't settled (FR-3). The UI pretty-prints with 2-space indentation.
- [ ] The noul default descriptions come from E09 student code, not a contract. They are one constant in `label.js`.

## Waiting on the owner (requirements §11)

- [x] Q3: the ten §1.4 keys are final; `out_of_scope` is dropped (2026-10-05).
- [x] Q7: `stale_state` is judged against `e13.asof`, defaulting to today, with no world knowledge. Every payload carries `asof` and the UI shows it.
- [x] Q8: `false_premise` is judged against the state only (definition updated; the refuting span stays required).
- [x] Q10: 3 is ideal; 1 must work. Overlap 1..n (default 3), a reliability subset, and re-label batches (schema v5).
- [ ] Q2: the keep rule; Q9: the boundary order for ambiguous/underspecified/subjective.
- [x] Import sample: `feature/e13-test-fixtures`, merged with its JSONL (sha256 matches the README).
- [x] Jev = an exact list, default `jev` (`openjev` is not Jev). Jev output marks the whole row's **release** tier; **visibility** follows the text's licence only (schema v4 `items.visibility`), except in batches that show Jev's answer.
