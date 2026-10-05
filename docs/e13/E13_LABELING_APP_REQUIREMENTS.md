# E13 labelling app: requirements

**Status:** draft, 2026-10-05. Nothing is built yet.

> **Owner decision (2026-10-05): development happens in a fork of `nli-span-labeler`, on a feature branch.** This
> supersedes the *location* part of §3.5 (`apps/e13_labeler/` in the ModernBERT repo) and §11 Q1. The findings in
> §3.4 and §3.5 still stand as design constraints: E13 needs its own data model (state + typed question,
> character-offset / JSON-pointer spans), blind labelling with no gold shown, Krippendorff's α, and permission tiers.
> So build E13 as a new module or app inside this repo; don't retrofit the premise/hypothesis tables.
>
> **Paths:** `docs/v3/...`, `experiments/v3/...` and `data/v3/...` refer to the
> ModernBERT-NLI-Advanced repo (on the owner's git server, not GitHub), not this one.
> `source_permissions.json` (source → licence tier, web-verified 2026-10-05) is copied beside this file for
> convenience. Pool rows (`data/v3/e09/pool_eval.jsonl`) are not copied, because some sources are restricted.
> Ask the owner for an import sample.
> **Owner decisions (2026-10-05, from the model-dev session).** These override the body text where they differ:
> 1. **Jev teachers (FR-8):** only `jev` (the TypeSafe API), as an explicit list matched exactly
>    (`E13_JEV_TEACHERS`, default `jev`). `openjev` is a local Apache-licensed model, not Jev. The E09 teachers are
>    `jev`, `clef`, `clefflash`, `decider`, `laya`, `nimble`, `openjev` and `semif`.
> 2. **Release tier vs visibility (§8.1, FR-6, FR-56, FR-57):** `permissions` is the *release* tier of a row's
>    labels. Any Jev output on a row marks every item of that row `jev` / `jev+restricted`. *Who may see* an item
>    depends only on the text's licence: `libre` text goes to `public` labelers; `restricted`/`unverified` text is
>    internal-only. Jev output doesn't hide an item, because labelers never see teacher outputs. Exception: a batch
>    that shows Jev's answer (`show_model_answer`, FR-34) is internal-only for the items carrying it. Jev outputs are
>    released with the notice; the tier exists so people can filter them out. This replaces the "who may see it"
>    column of the §8.1 table for `jev`.
> 3. **Reason list (§1.4, §11 Q3):** the ten keys of §1.4 are final, in that order. `out_of_scope` is dropped: it
>    describes the model, and `model_uncertainty` covers it. The renames are confirmed. `conflicting_evidence` is
>    ticked explicitly and needs ≥1 support and ≥1 refute span. `answerable` stays a separate flag.
> 4. **`stale_state` (§11 Q7) and `false_premise` (§11 Q8):** `stale_state` is judged against the question's own time
>    reference: `e13.asof` when set, otherwise today. Labelers ask whether the information *could* have changed,
>    never whether it did (no world knowledge); the dated or time-sensitive phrase is the required span.
>    `false_premise` is judged against the state only. The app shows every item's as-of date, so generated items
>    don't stand out, and stores the date shown with each annotation. `conflicting_evidence` and `stale_state` span
>    rules are hard: neither a batch's span policy nor the Shift+Enter override relaxes them.
> 5. **Overlap (§11 Q10, FR-31, FR-32), from the owner directly in the app session:** "3 is ideal, but we might have to make 1 work." `overlap_target` is
>    1..n, default 3 (this replaces "default 2, minimum 2"). With overlap 1, agreement comes from (a) a deterministic
>    **reliability subset** (`reliability_fraction` of items labelled by `reliability_overlap` ≥ 2 people), (b) a
>    **re-label batch** (`relabel_of`): the same labeler labels a sample of their own items again, blind, after
>    `relabel_after_days`, the only exception to "never see an item twice", which gives *intra*-rater α, reported
>    apart from inter-rater α; and (c) human-vs-committee α (FR-9). Single-labelled items are training data, not α data.
**Experiment:** E13 abstain-reason label quality (`ROADMAP_V3.md` §7).
**Related:**
- `ROADMAP_V3.md` §2 (abstention sources) and §7 (the E13 entry);
- `API_CONTRACT.md` §2 rule 7 (evidence coordinates), rule 9 (option-side spans) and rule 10 (names and meanings);
- `RELEASE_POLICY.md` §2 (the `permissions` column) and `DATASET_INVENTORY.md` (per-source licences);
- `experiments/v3/release/source_permissions.json` (source → `libre` | `restricted` | `unverified`);
- the Claude Doc "mbnli v3 output semantics: research questions" (owner decisions of 2026-10-04, summarised in memory `reference_output_semantics_doc.md`).

Keywords: **MUST** = needed for E13 to be valid; **SHOULD** = expected, may slip one milestone; **COULD** = nice to have.

---

## 1. Purpose and E13 background

### 1.1 What the student must return

The v3 student returns an abstention object for every answer (`API_CONTRACT.md` §2 rule 10, §3):
- `p`, the overall abstention probability;
- `reasons`, **independent per-reason probabilities** (sigmoids, not a softmax);
- `evidence`, the spans that triggered the reasons;
- a separate `model_uncertainty`.

`ROADMAP_V3.md` §2 lists the principled abstention sources: irrelevance, unresolved (the state doesn't settle the question), conflict, and difficulty. The output-semantics review added non-factual framing, ambiguity, false premise, "no option fits" and out-of-scope. The owner also proposed two further candidates, **`stale_state`** and **`subjective`**, but decided on 2026-10-04 that they go in only if their labels prove reliable. The owner's warning was that "more labels with subjective labelling can hurt".

### 1.2 What E13 tests

E13 (`ROADMAP_V3.md` §7, line 200) runs in four steps:
1. Generate candidate rows for every reason.
2. Label each reason twice: with two committee prompts, **and with a human sample**. Measure agreement per reason with Krippendorff's α.
3. Train the reason heads with and without the two candidates. Compare per-reason AUROC/F1 on the other reasons.
4. Keep a candidate only if its α is at the level of the established reasons and the other reasons don't lose.

### 1.3 What this app is for

This app produces the **human sample** in step 2, plus the human reference that the committee prompts are scored against. It must:
- collect independent, blind, double (or more) human labels per item for the ten reasons, with the evidence spans that triggered them;
- compute per-reason α inside the app, with confidence intervals, and export everything for offline recomputation;
- export labels in a training-ready schema that uses the served field names (`mbnli.abstain.reasons`, evidence roles), so the same rows can supervise the reason heads in step 3;
- enforce data-licence tiers, because some sources and all Jev outputs may only be shown to authorised internal labelers.

E07 showed that human spans beat teacher spans for evidence (`experiments/v3/e07_dspy_evidence/REPORT.md`: mixing in teacher labels lowered AP from 0.865 to 0.846 at 40k). The app's span output is therefore training data in its own right, not just audit data.

### 1.4 Label set

The labeler marks reasons per item, as independent multi-label flags.

| key | short definition shown to labelers | status in E13 |
|---|---|---|
| `unrelated` | The state is not about what the question asks (topicality, `API_CONTRACT.md` rule 10: "relevance means topicality, not answerability"). | established |
| `not_enough_info` | On topic, but the state doesn't settle the question ("unresolved", `ROADMAP_V3.md` §2). | established |
| `conflicting_evidence` | The state both supports and refutes the same option. | established |
| `non_factual_support` | The only support is hedged, attributed, hypothetical or quoted. **Negation does not count**: a negated fact is a refutation. | established (from the semantics review) |
| `stale_state` | The information may be out of date for what the question asks. | **candidate** |
| `ambiguous` | The question has several plausible readings that would get different answers. | established |
| `underspecified` | The question lacks a parameter it needs (whose, when, which unit, compared with what). | established (definition to confirm, §11) |
| `false_premise` | The question presupposes something the state contradicts or that is false. | established |
| `no_option_fits` | The state settles the matter, but none of the offered options matches. | established |
| `subjective` | The answer depends on taste or opinion rather than on the state. | **candidate** |

There are two further labels:
- **`answerable`**: an explicit "no abstain" choice. It is mutually exclusive with all reasons. It exists so that "no reason applies" can be told apart from "labeler didn't consider it".
- **`note`**: free text.

---

## 2. Users and roles

| role | who | can |
|---|---|---|
| `owner` | the project owner (one account, created at install from the command line, never hard-coded) | everything; grants clearance; sees identities |
| `admin` | trusted internal helper | import, batches, dashboards, adjudication, export; cannot change clearances or see the identity map |
| `labeler` | internal or external person | onboarding, labelling, their own stats only |
| `model` (pseudo-labeler) | a committee prompt or teacher (for example `committee_prompt_a`, `clefflash`) | no login. Labels are imported, never shown to humans by default, and used in α and dashboards |

Every human account carries a **clearance**:
- `public`: may see `libre` items only. This is the default for external labelers.
- `internal`: may see every tier.

The owner sets clearance per account; nothing else can raise it (§8).

---

## 3. Findings about the existing front end, and a recommendation

### 3.1 Where it is

| path | what it is |
|---|---|
| **`/storage/nli-span-labeler/`** | **The "modernbert-nli-advanced front end".** A standalone git repo (`git@github.com:jmccardle/nli-span-labeler.git`), 42 commits, built 2025-12-08 to 12-13 as a "GoblinCorps" multi-agent project (its `CLAUDE.md`). Its inventory note is `nli-span-labeler-INVENTORY-2026-04-05.md`. |
| `/storage/ModernBERT-NLI-advanced/labeler/` | Its single-user predecessor (Nov 2025, commit `5290593`): a 21 KB `app.py` with no auth, a `labels.db` and `static/index.html`. Superseded. |
| `/storage/ModernBERT-NLI-advanced/scripts/human_labeler.py` | A CLI labelling script from v1/v2. |
| `/storage/podsim/mbnli/labeler/` | A mirror copy of the repo's `labeler/` (pod simulation bundle). |
| `/storage/nli_server/` | Model servers (argument mining, REBEL, T5 claims), not a labelling UI. |

Search method: I ran `find /storage -maxdepth 4` for names matching `*modernbert*`, `*mbnli*`, `*annotat*`, `*label*`, `*frontend*` and `ui`, pruning `/storage/huggingface`, venvs, `node_modules` and `.git`, and then read the matches.

### 3.2 Stack and how it runs (`/storage/nli-span-labeler`)

- **Backend:** FastAPI + Uvicorn in one 5,362-line `app.py` (208 KB). Pydantic v2 models. The interactive API docs are at `/docs`.
- **Frontend:** one 5,175-line vanilla-JS `static/index.html`, with no build step.
- **Storage:** SQLite `labels.db`. Tables (`app.py:771-1030`): `users`, `sessions`, `examples`, `example_locks`, `labels`, `span_selections`, `complexity_scores`, `skipped`, `flagged_examples`, `example_agreement`, `annotator_agreement`, `gold_annotations`, `training_submissions`, `auto_spans` and `tier1_quality`. Migrations are ad hoc `migrate_*` functions.
- **Auth:** username + password, with a session token in a cookie (`samesite="lax"`, `app.py:2251`; no `secure=` flag found). Passwords are salted **SHA-256** (`app.py:1834-1846`). Roles are annotator/admin, and admin is bootstrapped from the `ADMIN_USER` env var. `ANONYMOUS_MODE=1` gives single-user mode.
- **Run:** `./run.sh` → `uvicorn app:app --reload --port 8000 --host 0.0.0.0`. The data is SNLI/MNLI/ANLI JSONL in `data/nli/`, from `scripts/download_data.py`. Tests: `pytest tests/` (auth, admin, annotation, examples), with CI in `.github/workflows/test.yml`.
- **Use so far** (read-only count of `labels.db`): 3 users, 300 examples loaded, 7 labels and 5 complexity-score rows. That is effectively unused, so there is no data worth migrating.

### 3.3 What it does today

- Word-level span labelling of NLI premise/hypothesis pairs. Words come from the ModernBERT tokenizer (`TOKENIZER_MODEL`), and labels are stored as word indices plus character offsets.
- Six "difficulty" sliders scored 0–10, NLI relation labels, and custom labels.
- Tier 0 (rule) and Tier 1 (WordNet) auto-spans.
- Multi-annotator support with 30-minute locks, calibration routing, a gold/training mode for new users, flagging of controversial items, a reliability leaderboard, and JSONL/CSV export.
- Calibration routing (`app.py:4395-4465`, env `CALIBRATION_TEST_RATIO_NEW=0.8`, `..._ESTABLISHED=0.1`):
  - provisional users get mostly high-consensus "test" items;
  - calibrated users get about 10% test items for drift detection;
  - otherwise items are served in pool order: `zero_entry` → `building` → `test`.

### 3.4 Fit for E13

| E13 needs | nli-span-labeler | gap |
|---|---|---|
| state as text **or JSON**; typed question (`noul`/`choice`/`score`) with options | `examples` has `premise TEXT NOT NULL, hypothesis TEXT NOT NULL` (`app.py:816-826`) | schema rewrite |
| 10 independent reason flags plus `answerable` and a note | difficulty sliders and NLI labels | new |
| character offsets and JSON pointers; roles support/refute/unsupported/framing; option-side spans | word indices from a tokenizer, premise/hypothesis only | new span model |
| blind labelling | `/api/next` returns `gold_label` (`app.py:4489`) and the UI **displays it** (`static/index.html:3142-3147`) | must be removed |
| per-reason Krippendorff's α | complexity agreement is "1 − MAD/5", span agreement is pairwise Jaccard (`app.py:1274-1373`); no α anywhere | new |
| permission tiers per item, clearance per labeler | none | new |
| "never see an item twice", overlap targets | exclusion keyed on `complexity_scores`/`skipped` (`app.py:4411-4422`); no overlap target, since pools close at a consensus count | rewrite |
| minimal identity; strong password hashing | username plus SHA-256 | upgrade |
| time per item | none | new |

**What is worth reusing (as ideas or copied code, not as a fork):**
- the session and lock pattern (`acquire_lock`/`release_lock`, `app.py:1706-1760`);
- the calibration-routing idea (hidden gold at a configurable rate, high for new labelers);
- the training/gold mode flow (`/api/training/*`, `app.py:2797-3070`);
- the flagged-item admin flow;
- the keyboard handler skeleton (`static/index.html:2578-2640`).

### 3.5 Recommendation: build a separate app, on the same stack, beside the old one

**Do not extend `nli-span-labeler`.** Build a new, small app, `e13_labeler`, with the same stack (FastAPI + SQLite + one static page; optionally Alpine.js or plain JS, with no build step). Lift the patterns listed above. Reasons:
1. **The core data model is wrong for E13.** Premise/hypothesis is baked into the tables, the API (`ExampleResponse`) and about 5k lines of UI. Changing it touches nearly everything, so "extending" would really be a rewrite inside a 208 KB monolith.
2. **Its blindness and agreement logic would have to be removed rather than reused.** It shows gold labels, and it treats high consensus as a routing pool, not a measured statistic.
3. **There is no data to preserve** (5 annotated examples), and the repo belongs to a different team workflow (the GoblinCorps PR roles in its `CLAUDE.md`).
4. **The same stack keeps the owner's habits:** `run.sh`, pytest, SQLite, and a single page that runs on midlife or a laptop. The E09 pool files and `source_permissions.json` are Python-side, so a Python backend reads them directly.

**Location (proposed; see §11 Q1):** `/storage/ModernBERT-NLI-advanced/apps/e13_labeler/`, with its database under a git-ignored path such as `outputs/e13_labeler/e13.db`. This keeps it next to the pool data and the permissions file, and lets the E13 training scripts import its export module. `nli-span-labeler` stays as it is, untouched.

---

## 4. Functional requirements

Each requirement is testable. "Test" names the acceptance check.

### 4.1 Items and import

- **FR-1 (MUST)** Import JSONL in the E09 pool format (`data/v3/e09/pool_eval.jsonl`): `{id, source, split, heldout, state, questions: {qid: {type, instructions?, criteria?}}, gold: {qid: answer}}`. Test: importing the first 100 rows of `pool_eval.jsonl` creates one item per (row, qid), and 0 rows are rejected.
- **FR-2 (MUST)** One **item = one (state, question)**, with `item_id = "<row id>#<qid>"`. Other questions of the same row are not shown, because v3 answers are question-invariant (`ROADMAP_V3.md` §3). Test: a row with 4 questions (for example `typed_decisions/security_incidents_000085`) yields 4 items.
- **FR-3 (MUST)** Question types are rendered as follows:
  - `choice`: criteria are a dict of `{option: description}`;
  - `score`: criteria are a list, and levels are shown as `0..n-1` with their descriptions;
  - `noul`: optional `{true, false}` descriptions, with defaults shown when they are missing;
  - a missing `instructions` field falls back to the qid (`API_CONTRACT.md` §4);
  - dict or list `instructions` or criteria values are rendered as pretty JSON, not as a Python repr.

  Test: fixtures for each case render without error.
- **FR-4 (MUST)** Detect the state format. A state is `json` if it is a JSON object or array, or a string that parses as one (E09 stores `typed_decisions` states as JSON-encoded strings). Otherwise it is `text`. An import field `state_format` overrides the detection. Test: `typed_decisions/*` items are `json`, and `bbc_news/*` items are `text`.
- **FR-5 (MUST)** Store the state **exactly as imported**, plus `state_sha256`, so that span offsets refer to the caller's string (`API_CONTRACT.md` rule 7). Test: export → re-import round-trips byte-identical states and hashes.
- **FR-6 (MUST)** Every item gets a `permissions` tier (§8), resolved at import:
  1. an explicit `permissions` field on the row;
  2. else `source_permissions.json[source].class`;
  3. `unverified` and unknown sources map to `restricted`;
  4. the tier is raised to `jev`/`jev+restricted` if the item carries any Jev output (FR-8).

  Test: a row from an unknown source imports as `restricted`.
- **FR-7 (MUST)** Optional E13 import fields: `e13.candidate_for` (the reasons the generator targeted), `e13.generator` (script, version), `e13.asof` (the date the question is asked "as of", for `stale_state`, §11 Q7), and `e13.batch`. The generator's target **is never shown to labelers**. Test: the labeler API response contains no `e13` key.
- **FR-8 (SHOULD)** Optional `model_answers: {teacher: answer}` in the raw-teacher format (`data/v3/e09/raw/<teacher>.eval.jsonl`). Stored hidden; shown only in batches with `show_model_answer` (FR-34). Test: importing with a `jev` answer sets the item's tier to `jev`.
- **FR-9 (SHOULD)** Import committee or teacher reason labels as `model` pseudo-labelers (JSONL as in §5.4 with `labeler_kind: "model"`). Used for human-vs-committee and prompt-A-vs-prompt-B α (E13 step 2). Test: α between two imported pseudo-labelers equals the reference implementation (FR-40).
- **FR-10 (MUST)** Imports are idempotent: re-importing the same row id with the same `state_sha256` is a no-op, and a different hash is rejected unless `--replace` is given. Every import writes an `import_runs` record (file, sha256, counts, user). Test: double import → identical item count.
- **FR-11 (MUST)** Import is a CLI command (`python -m e13_labeler import FILE --batch NAME`) and an admin upload page.

### 4.2 Labelling a reason item (task type `reasons`, the default)

- **FR-12 (MUST)** The screen shows the state, the question (instructions plus all options, in their original order) and the reason checklist (§6.1). The source name, dataset gold, generator target, model answers and other people's labels are hidden. Test: the `GET /api/next` payload for a `labeler` contains none of `gold`, `source`, `e13`, `model_answers` or `labels_of_others`.
- **FR-13 (MUST)** The ten reasons are independent toggles, and `answerable` is exclusive: checking it clears every reason, and checking any reason clears it. Submitting requires `answerable` or at least one reason. Test: the API rejects an empty submission and one with both.
- **FR-14 (MUST)** A free-text note (≤ 2,000 chars), optional by default. A batch may require a note when `ambiguous`, `underspecified` or `subjective` is checked (FR-33). Test: config on → a submission without a note is rejected.
- **FR-15 (MUST)** Evidence spans. The labeler selects text in the state or in an option and gives it a role:

  | role | side | meaning (`API_CONTRACT.md` rule 10) |
  |---|---|---|
  | `support` | state or option | rules the option in |
  | `refute` | state or option | rules the option out |
  | `unsupported` | **option only** | option content the state doesn't back |
  | `framing` | state | the hedge, attribution, conditional or quote marker that makes support non-factual (`non_factual_support`) |

  Test: the API rejects `unsupported` with `side: "state"` and `framing` with `side: "option"`.
- **FR-16 (MUST)** Each span links to the option it is about (`option`; null for `noul` true/false is stored as `"true"`/`"false"`) and to **one or more reasons** it triggered (`reasons: [...]`). It may link to no reason, as plain evidence. Test: a span with a reason that isn't checked is rejected.
- **FR-17 (MUST)** Coordinates:
  - text states: `start`/`end` character offsets into the state string;
  - JSON states: an RFC 6901 `pointer` plus offsets inside that string value, or the pointer alone for a key or whole scalar (`API_CONTRACT.md` rule 7);
  - option-side spans: `side: "option"`, the option name, and offsets into the option's description string (rule 9).

  The stored `text` must equal the slice. Test: for every stored span, `state[start:end] == text` (or the pointer target's slice).
- **FR-18 (MUST)** Selections snap to word boundaries by default. E07 found that annotators mark single words and that word units beat phrases (`e07_dspy_evidence/REPORT.md` finding 2). Holding `Alt` while selecting gives character precision. Test: dragging across "playi|ng a gui|tar" stores "playing a guitar".
- **FR-19 (SHOULD)** Per-batch span policy per reason: `required` | `optional` | `none`. Proposed defaults:
  - `conflicting_evidence`: required, with ≥1 support and ≥1 refute on the same option. *Owner decision 2026-10-05: a hard rule that no batch policy or override relaxes;*
  - `non_factual_support`: required, with ≥1 framing and ≥1 support;
  - `stale_state`: required (the dated or time-sensitive phrase). *Hard rule, as for `conflicting_evidence`;*
  - `false_premise`: required (the refuting span). *Owner decision 2026-10-05 (§11 Q8): judged against the state only, so there is no world-knowledge case;*
  - `unrelated`: none;
  - all others: optional.

  The UI blocks submit with a message, and `Shift+Enter` overrides and records `policy_override: true`. Test: with the defaults, `conflicting_evidence` and no spans → blocked; override → saved with the flag.
- **FR-20 (MUST)** Skip, with a required reason code: `cannot_judge`, `broken_item`, `offensive`, `too_long` or `other`, plus an optional note. A skipped item is never served to that labeler again and counts as neither label nor α data. Test: after a skip, `next` never returns that item to the same user.
- **FR-21 (SHOULD)** A labeler can go back to and edit their own last 20 submissions until the batch is closed. Every edit creates a new annotation version, the old one is kept, and α uses the latest. Test: an edit produces version 2, and the export has `version: 2`.
- **FR-22 (MUST)** Time per item:
  - **active time**: the client measures from render to submit, counting only while the tab is visible and the user has interacted in the last 60 s;
  - **wall time**: measured by the server.

  Both are stored per annotation version. Test: hiding the tab for 5 min adds < 1 s of active time.

### 4.3 Optional task type `relation`

- **FR-23 (SHOULD)** Batch task type `reasons+relation` (or `relation` alone). For each option the labeler picks a stance of `support` / `refute` / `undetermined`, and for the question a `relevance` of `relevant` / `not_relevant` (topicality). For `noul`, the options are `true` and `false`. Test: in a `reasons+relation` batch, submission requires a stance on every option.
- **FR-24 (SHOULD)** Consistency checks, warned about rather than blocked:
  - `not_relevant` without `unrelated`, or the reverse;
  - `conflicting_evidence` without some option having both support and refute spans;
  - every option `refute` without `no_option_fits` or `false_premise`.

  Test: each check fires on a fixture.
- **FR-25 (COULD)** Score questions in relation mode use threshold stance ("at least level k": support/refute/undetermined per k), matching `API_CONTRACT.md` rule 6. This is off by default because §6 of the contract leaves it open.

### 4.4 Onboarding, gold and calibration

- **FR-26 (MUST)** A guideline page, versioned (`guideline_version`), that a labeler must open before the quiz. For each reason it gives the definition, one positive example, one near-miss negative, and the span rule. Every annotation records the guideline version in force. Test: a new account cannot reach `/label` before viewing the guideline.
- **FR-27 (MUST)** Quiz: N gold items (default 12, configurable), at least one per reason including both candidates, plus 2 `answerable` items. After each answer the labeler sees immediate feedback: the gold reasons, the gold spans and the explanation. The labeler passes when:
  - per-item reason-set accuracy (exact match, or Jaccard ≥ 0.8) is ≥ 75% overall; and
  - no established reason is missed on more than one of its quiz items.

  On a fail, the labeler can retake the quiz once after re-reading the guideline. A second fail sends the account to the owner for review. Test: a scripted user with all-correct answers passes, and one who never checks `conflicting_evidence` fails.
- **FR-28 (MUST)** Hidden gold during labelling: served at `gold_rate_new` (default 0.20) for a labeler's first 50 items, and `gold_rate` (default 0.05) after that. Gold items look like ordinary items, give no feedback by default, and are **excluded from α and from training export**. Test: over 1,000 simulated `next` calls after warm-up, the gold share is 5% ± 2%.
- **FR-29 (MUST)** Gold management for admins: create gold from scratch, promote an adjudicated item to gold (§7), edit it, retire it, and attach an explanation. Gold stores the reasons, the spans, and optional per-reason "acceptable alternatives" (for example, `ambiguous` or `underspecified` both accepted). Test: CRUD API tests.
- **FR-30 (SHOULD)** Per-labeler gold accuracy per reason. When a labeler's rolling 30-item gold accuracy falls below a threshold (default 0.6), the dashboard flags them and their status becomes `paused`. `next` then returns the retraining quiz. Test: a simulated labeler failing 10 gold items in a row gets paused.

### 4.5 Batches, queues and assignment

- **FR-31 (MUST)** A batch is a named set of items with: `task_type`, `reason_set` (FR-33), `overlap_target` (*owner decision 2026-10-05: default 3, minimum 1*; plus an optional reliability subset and re-label batches, see the decisions block), `tier_ceiling`, the span policy (FR-19), `show_model_answer` (default false), `priority` and a status of `draft`/`open`/`closed`. Test: `overlap_target < 1` is rejected; overlap 1..3 each yield exactly that many labels per item.
- **FR-32 (MUST)** `GET /api/next` picks an item where all of these hold:
  - the batch is open;
  - the item's tier ≤ the labeler's clearance (FR-50);
  - the labeler has never labelled or skipped it (in any batch: the key is `item_id`, not the batch);
  - it isn't locked by someone else;
  - it is below its overlap target.

  It then orders the candidates:
  1. items that already have labels from others but are below target (to **complete pairs first**, so α accrues early);
  2. then by batch priority;
  3. then randomly.

  Lock timeout: default 20 min. Test: property test with 5 simulated labelers over 200 items: no labeler gets an item twice, and every item ends with exactly `overlap_target` labels.
- **FR-33 (SHOULD)** `reason_set` per batch: the reasons shown, and their order. The default is all ten, in §1.4 order. This allows a human-side dilution test: one batch without the two candidates, one with, on disjoint labelers or items (§11 Q11). Test: a batch with 8 reasons never renders `stale_state` or `subjective`, and its export marks them `null` (not asked), not `false`.
- **FR-34 (COULD)** `show_model_answer: <teacher>` shows that teacher's chosen answer, labelled "a model's answer", for audit-style batches. Items with a Jev answer are tier `jev` (FR-8). α from such batches is reported separately and never pooled with blind batches. Test: the dashboard keeps them as separate rows.
- **FR-35 (MUST)** Progress: per batch (items at 0/1/2/≥target labels; % complete), per labeler (done today and in total; median active time), and an ETA at the current pace. Test: the counts match the database after a simulated session.
- **FR-36 (SHOULD)** States repeat across the questions of one row. Within one labeler's queue, items of the same row may be served in a row to save reading time, as a batch option. Order effects are then logged (`position_in_state_run`). Test: the option is on → consecutive items share `row_id` when available.

### 4.6 Agreement, QA and adjudication

- **FR-37 (MUST)** Per-reason Krippendorff's α (nominal, binary present/absent), computed over items with ≥ 2 human labels, in blind batches, excluding gold. It is reported with: the number of items, the number of pairable values, prevalence, and a 95% bootstrap CI (1,000 resamples over items). Test: matches the `krippendorff` PyPI package to 1e-6 on a fixture (FR-40).
- **FR-38 (MUST)** Further α variants:
  - **"any abstain"** (`answerable` vs not);
  - **the full reason set**, with MASI distance (SHOULD);
  - **human vs model pseudo-labeler and model vs model** (FR-9), per reason. This covers E13 step 2's "two committee prompts".

  Test: fixture values.
- **FR-39 (SHOULD)** Span agreement per role, and per reason trigger:
  - token-level F1 and Jaccard between labelers, on word-snapped spans;
  - E07's AP metric against the other labelers, where three or more exist.

  Unitized α (α_U) is COULD. Test: fixture values.
- **FR-40 (MUST)** The α code is one function in a module that both the app and the offline E13 analysis import, with a unit test against the reference package. Test: as stated.
- **FR-41 (MUST)** Agreement dashboard (§6.4). It gives:
  - per-reason α with CI, prevalence and n, with the candidates highlighted;
  - the comparison line "candidate α vs the min and median of the established reasons' α";
  - a reason co-occurrence and confusion matrix between labeler pairs (for example, A says `stale_state` where B says `not_enough_info`), which shows dilution;
  - per-labeler gold accuracy and pairwise agreement.

  Test: the dashboard API returns those fields.
- **FR-42 (SHOULD)** Low-prevalence warning: a reason with fewer than 30 positive judgements, or prevalence < 3%, shows "α unstable". Test: a fixture triggers the warning.
- **FR-43 (MUST)** Adjudication queue. It holds items where labelers disagree on any reason or on `answerable`, sorted by disagreement count. An internal labeler with the `admin` role sees all labels side by side (anonymised as L-a, L-b), picks the final reasons and spans, and may promote the item to gold. Adjudication is stored separately and **never** overwrites raw labels; α always uses the raw labels. Test: after adjudication, α is unchanged and the export has `adjudicated` filled.
- **FR-44 (SHOULD)** Labelers can flag an item ("bad item", "guideline unclear") from the labelling screen. Flags go to an admin list. Test: a flag is created and visible.

### 4.7 Export

- **FR-45 (MUST)** Annotation export: one JSONL row per (item, labeler, latest version) in the §5.4 schema. Pseudonymous labeler ids only. Test: the schema validates with the JSON Schema shipped in the app.
- **FR-46 (MUST)** Training export: one JSONL row per item with ≥ `overlap_target` labels, in the §5.5 schema. It contains:
  - soft reason targets (the fraction of labelers);
  - the majority and adjudicated reason sets;
  - merged spans with vote counts;
  - permissions and provenance.

  The field names follow `API_CONTRACT.md` §3 (`abstain.reasons`, evidence `side`/`pointer`/`start`/`end`/`role`/`option`). Test: every exported span satisfies FR-17, and the reason keys are a subset of the batch `reason_set`.
- **FR-47 (MUST)** Export filters: batch, `permissions` (exact values, as in `RELEASE_POLICY.md` §2, so the release recipes can filter), date range, include/exclude model pseudo-labelers, include gold. Test: `--permissions libre` exports only libre rows.
- **FR-48 (MUST)** Agreement export: a JSON document with every number on the dashboard, plus the inputs (item ids and labeler ids) used, so the result can be reproduced offline. Test: recomputing offline from the export reproduces α exactly.
- **FR-49 (SHOULD)** Exports are written under `outputs/e13_labeler/exports/<timestamp>/` with a manifest (app git commit, guideline version, DB checksum, filters). Nothing is ever deleted (owner policy: keep all artifacts). Test: a manifest exists and lists the files.

### 4.8 Access control and labeler management

- **FR-50 (MUST)** Clearance is enforced **server-side** on every endpoint that returns item content (`next`, `item/{id}`, `history`, `quiz`, `export`). A `public` labeler gets 404 for any item above `libre`, even by direct id. Test: a `public` user requesting a `restricted` item id gets 404.
- **FR-51 (MUST)** Accounts are created by invite only. The owner or an admin creates an invite link (single use, expires in 7 days) bound to a role and clearance. Self-registration is off. Test: registering without a token → 403.
- **FR-52 (MUST)** Labeler management: list, pause, resume, revoke (ends sessions), change clearance (owner only), reset password, and view per-labeler stats. Test: revoking invalidates existing sessions immediately.
- **FR-53 (MUST)** An audit log of logins, clearance changes, exports, imports, adjudications and gold edits, with actor, time and target. Test: an export creates a log row.
- **FR-54 (SHOULD)** `SINGLE_USER=1` mode for the MVP: one owner account, auto-login on localhost only, and every other rule unchanged. Test: requests from a non-loopback address are refused in this mode.

---

## 5. Data model and import/export schemas

The examples in §5.2–§5.5 are illustrative: the field names and shapes are normative, but offsets, hashes and fields such as `/alert/timestamp` are made up.

### 5.1 Tables (SQLite, WAL mode)

```
labelers(id PK, pseudonym UNIQUE "L07", kind human|model, role owner|admin|labeler|model,
         clearance public|internal, status invited|onboarding|active|paused|revoked,
         login_name UNIQUE NULL, password_hash NULL (argon2id), created_at, last_seen)
identity(labeler_id PK→labelers, contact TEXT NULL, notes TEXT NULL)       -- owner-only; optional; deletable
invites(token_hash PK, role, clearance, expires_at, used_by NULL)
sessions(token_hash PK, labeler_id, expires_at)

items(item_id PK "<row_id>#<qid>", row_id, qid, source, split, heldout,
      state TEXT, state_format text|json, state_sha256, question_json, gold_json NULL,
      permissions libre|restricted|jev|jev+restricted, source_license, e13_json NULL,
      model_answers_json NULL, import_run_id)
batches(id PK, name UNIQUE, task_type, reason_set_json, overlap_target, tier_ceiling,
        span_policy_json, show_model_answer NULL, priority, status, guideline_version)
batch_items(batch_id, item_id, PK(batch_id,item_id))
locks(item_id PK, labeler_id, until)

annotations(id PK, item_id, batch_id, labeler_id, version, answerable BOOL,
            reasons_json {"unrelated":true,...,"subjective":null}, note, relation_json NULL,
            skipped_code NULL, policy_override BOOL, is_gold_probe BOOL,
            active_ms, wall_ms, position_in_state_run, guideline_version, created_at,
            UNIQUE(item_id, labeler_id, version))
spans(id PK, annotation_id, side state|option, option NULL, pointer NULL, start NULL, end NULL,
      text, role support|refute|unsupported|framing, reasons_json [..])

gold(item_id PK, reasons_json, spans_json, alternatives_json, explanation, created_by, retired BOOL)
quiz_attempts(id PK, labeler_id, guideline_version, items_json, score, passed, created_at)
adjudications(item_id PK, batch_id, reasons_json, spans_json, adjudicator_id, created_at)
flags(id PK, item_id, labeler_id, kind, note, status)
import_runs(id PK, file, sha256, n_rows, n_items, n_rejected, actor, created_at)
audit_log(id PK, actor_id, action, target, detail_json, created_at)
```

How the reason flags are stored: in `reasons_json`, `true` means present and `false` means asked and absent. `null` means the reason wasn't in the batch's `reason_set`, so that labeler wasn't asked. α treats `null` as missing data, not as absent.

### 5.2 Import row (the E09 pool format plus optional E13 fields)

```json
{"id": "typed_decisions/security_incidents_000085",
 "source": "typed_decisions", "split": "eval", "heldout": false,
 "state": "{\"alert\": {\"description\": \"many failed authentications followed by a success\", \"evidence\": \"...\"}}",
 "questions": {
   "severity": {"type": "score", "instructions": "How severe is the potential impact if this alert is real?",
                "criteria": ["Negligible: ...", "Low: ...", "Moderate: ...", "High: ...", "Critical: ..."]},
   "credential_compromise": {"type": "noul",
                "instructions": "The evidence indicates a credential or account has been compromised."}},
 "gold": {"severity": 3, "credential_compromise": true},
 "permissions": "libre",
 "e13": {"candidate_for": {"severity": ["stale_state"]},
         "generator": "e13/gen_candidates.py@<commit>", "asof": "2026-10-05"},
 "model_answers": {"clefflash": {"credential_compromise": {"type": "noul", "noul": 0.91}}}}
```

`permissions`, `e13` and `model_answers` are optional. Everything else is exactly as in `data/v3/e09/pool_eval.jsonl`. A choice question looks like the `bbc_news/eval/73` row: `"criteria": {"business": "business", ...}`.

### 5.3 What the labeler API returns (blind)

```json
{"item_id": "bbc_news/eval/73#q0", "lock_until": "2026-10-05T14:20:00Z",
 "state": "disney settles disclosure charges ...", "state_format": "text",
 "question": {"type": "choice", "instructions": "What section of the news site does this article belong to?",
              "criteria": {"business": "business", "entertainment": "entertainment", "politics": "politics",
                           "sport": "sport", "tech": "tech"}},
 "reason_set": ["unrelated", "not_enough_info", "conflicting_evidence", "non_factual_support", "stale_state",
                "ambiguous", "underspecified", "false_premise", "no_option_fits", "subjective"],
 "task_type": "reasons", "span_policy": {"conflicting_evidence": "required", "...": "..."},
 "progress": {"batch": "e13-pilot-01", "done_by_me": 41, "batch_pct": 0.37}}
```

There is no `source`, `gold`, `e13`, `model_answers` or `is_gold_probe` field.

### 5.4 Annotation export (one per item × labeler)

```json
{"schema": "e13.annotation/1",
 "item_id": "typed_decisions/security_incidents_000085#severity",
 "row_id": "typed_decisions/security_incidents_000085", "qid": "severity",
 "batch": "e13-pilot-01", "labeler": "L03", "labeler_kind": "human", "version": 1,
 "guideline_version": "g1.2", "permissions": "libre", "state_sha256": "sha256:9f2c...",
 "answerable": false,
 "reasons": {"unrelated": false, "not_enough_info": false, "conflicting_evidence": false,
             "non_factual_support": false, "stale_state": true, "ambiguous": false,
             "underspecified": true, "false_premise": false, "no_option_fits": false, "subjective": false},
 "spans": [
   {"side": "state", "pointer": "/alert/evidence", "start": 0, "end": 34,
    "text": "An unrecognized external source re", "role": "support", "option": "3", "reasons": []},
   {"side": "state", "pointer": "/alert/timestamp", "start": null, "end": null,
    "text": "2023-04-02", "role": "support", "option": null, "reasons": ["stale_state"]}],
 "relation": null,
 "note": "Severity depends on whether the admin account was production; not stated.",
 "skipped": null, "policy_override": false,
 "timing": {"active_ms": 18450, "wall_ms": 23110},
 "created_at": "2026-10-05T14:02:11Z"}
```

Model pseudo-labelers use the same schema with `"labeler_kind": "model"` and `"labeler": "committee_prompt_a"`, which is how FR-9 imports them. A `relation` value, when present, looks like `{"relevance": "relevant", "stance": {"0": "refute", "1": "refute", "2": "undetermined", "3": "support", "4": "undetermined"}}`.

### 5.5 Training export (one per item, ready for the reason heads)

```json
{"schema": "e13.train/1",
 "id": "bbc_news/eval/73", "qid": "q0", "source": "bbc_news", "split": "eval", "heldout": true,
 "permissions": "restricted", "source_license": "mirror states none; upstream licence to trace",
 "text_included": true, "state_sha256": "sha256:...",
 "state": "disney settles disclosure charges ...",
 "question": {"type": "choice", "instructions": "...", "criteria": {"business": "business", "...": "..."}},
 "targets": {"mbnli": {"abstain": {
     "p": 0.0,
     "reasons": {"unrelated": 0.0, "not_enough_info": 0.0, "conflicting_evidence": 0.0,
                 "non_factual_support": 0.0, "stale_state": 0.5, "ambiguous": 0.0, "underspecified": 0.0,
                 "false_premise": 0.0, "no_option_fits": 0.0, "subjective": 0.0},
     "evidence": [{"side": "state", "start": 412, "end": 433, "text": "between 1999 and 2001",
                   "role": "support", "option": "business", "reasons": ["stale_state"], "votes": 1}]},
   "evidence": [{"side": "state", "start": 0, "end": 33, "text": "disney settles disclosure charges",
                 "role": "support", "option": "business", "votes": 2}]}},
 "human": {"n_labelers": 2, "labelers": ["L03", "L07"], "answerable_votes": 1,
           "majority": {"answerable": null, "reasons": []}, "adjudicated": null,
           "batch": "e13-pilot-01", "guideline_version": "g1.2", "reason_set": "all10"}}
```

How the export fields are filled:
- **`targets.mbnli.abstain.p`**: the fraction of labelers who did *not* choose `answerable`.
- **`reasons.<r>`**: the fraction of labelers who checked `r`. It is `null` if `r` wasn't in the batch's `reason_set`.
- **Majority ties**: these stay `null` in `majority`. Training scripts can then use the soft target or the adjudicated set.
- **Spans**: merged by exact coordinate match after word-snapping, with `votes` counting the labelers. The fields follow `API_CONTRACT.md` §3.
- **`text_included`**: `false` drops `state` and the question text and keeps the pointer fields (`id`, `qid`, `state_sha256`), as in `RELEASE_POLICY.md` §3.2.

---

## 6. Labelling UI wireframes

### 6.1 Main labelling screen (text state, `reasons` task); target 1366×768

```
+----------------------------------------------------------------------------------------------+
| E13 labeler   batch e13-pilot-01  [#######-----] 37%   me: 41 today  med 21s   L03  [?] [Guide]|
+----------------------------------------------------------+-----------------------------------+
| STATE                                         text  1,912c| QUESTION  (choice)                |
|                                                          | What section of the news site     |
| disney settles disclosure charges walt disney has settled| does this article belong to?      |
| charges from us federal regulators that it failed to     |                                   |
| disclose how family members of directors were employed   |  a  business                      |
| by the company. the media giant was not fined by the     |  b  entertainment                 |
| securities and exchange commission but has agreed to ... |  c  politics                      |
| ... between [1999 and 2001]s it employed three adult ... |  d  sport                         |
|                                     ^ support:a  stale   |  e  tech                          |
|                                                          +-----------------------------------+
|                                                          | ABSTAIN REASONS (toggle 1-0)      |
|                                                          | [ ] 1 unrelated                   |
|                                                          | [ ] 2 not_enough_info             |
|                                                          | [ ] 3 conflicting_evidence   *span|
|                                                          | [ ] 4 non_factual_support    *span|
|                                                          | [x] 5 stale_state  <active>  *span|
|                                                          | [ ] 6 ambiguous                   |
|                                                          | [ ] 7 underspecified              |
|                                                          | [ ] 8 false_premise               |
|                                                          | [ ] 9 no_option_fits              |
|                                                          | [ ] 0 subjective                  |
|                                                          | ( ) Space  answerable, no abstain |
+----------------------------------------------------------+-----------------------------------+
| SPANS  #1 state "between 1999 and 2001"  support -> a  [stale_state]          (Del removes)   |
| NOTE  [n] ________________________________________________________________                    |
| Enter save+next   Shift+Enter override   x skip   z undo   f flag   Alt-drag char-precise   |
+----------------------------------------------------------------------------------------------+
```

Notes on the layout:
- The candidates sit in their fixed key positions (5 and 0). The order is fixed per batch so that key positions stay stable. Changing it is a batch setting (FR-33).
- Spans are drawn with colour **and** an underline style per role, so roles are readable without colour:

  | role | colour | underline |
  |---|---|---|
  | support | green | solid |
  | refute | red | double |
  | unsupported | amber | dotted |
  | framing | blue | wavy |

### 6.2 Keyboard map (FR keyboard-first; nothing requires the mouse except drag-selecting, which also has a keyboard path)

| key | action |
|---|---|
| `1`..`9`, `0` | toggle reason 1..10; turning one on makes it the *active reason* that new spans link to |
| `Space` | toggle `answerable` (clears reasons) |
| `[` / `]` | cycle the active reason among the checked ones |
| `←`/`→` (`h`/`l`) | move the word cursor in the focused text; `Shift+←/→` extends the selection by word |
| `Tab` | move focus between state, options and the note |
| `s` / `r` / `u` / `m` | give the selection role support / refute / unsupported / framing ("m" for marker); then `a`..`z` picks the option the span is about (`t`/`f` for noul) |
| `Del` | delete the focused span |
| `n` | focus the note (`Esc` leaves) |
| `Enter` | save and next. `Shift+Enter` saves, overriding the span policy |
| `x` | skip (then `1`..`5` for the code) |
| `f` | flag the item |
| `z` | undo the last action |
| `?` | help overlay; `g` opens the guideline in a side panel |

The relation task (FR-23) adds a stance column per option (§6.3). There, `j`/`k` move between options and `s`/`r`/`d` set support / refute / undetermined.

### 6.3 JSON state, with relation mode

```
+----------------------------------------------------------+-----------------------------------+
| STATE                                     json  /alert   | QUESTION (noul)                   |
| alert                                                    | The evidence indicates a          |
|   description: "many failed authentications followed by | credential or account has been    |
|                 a success"                               | compromised.                      |
|   evidence:    "An unrecognized external source recorded |  t true  - (no description)       |
|                 [84 failed authentication attempts]s ... |  f false - (no description)       |
|                 [reportedly]m succeeded ..."              |                                   |
|   timestamp:   [2023-04-02]                              | RELEVANCE  (v) relevant ( ) not   |
|                                                          | STANCE  true:  [S] r  d           |
| pointer of focused token: /alert/evidence  [64..73]      |         false: s  [R] d           |
+----------------------------------------------------------+-----------------------------------+
```

The JSON view is pretty-printed for reading, but every span is stored as a pointer plus offsets into the original string value (FR-17).

### 6.4 Admin agreement dashboard

```
+----------------------------------------------------------------------------------------------+
| Agreement   batches: [e13-pilot-01 x] [e13-main-01 x]   blind only [x]   gold excl. [x]       |
+----------------------+------+-------------+------+------+------------------------------------+
| reason               |  n   | alpha       | CI95 | prev | vs established (min .61 / med .72) |
+----------------------+------+-------------+------+------+------------------------------------+
| unrelated            | 412  | 0.88        | .83-.92 | 11% |                                   |
| not_enough_info      | 412  | 0.69        | .61-.76 | 23% |                                   |
| conflicting_evidence | 412  | 0.72        | .60-.81 |  6% |                                   |
| ...                  |      |             |      |      |                                    |
| stale_state    CAND  | 412  | 0.41        | .27-.55 |  4% | BELOW min established  ! unstable  |
| subjective     CAND  | 412  | 0.66        | .55-.75 |  9% | within range                       |
| any abstain          | 412  | 0.79        | .74-.84 | 38% |                                   |
+----------------------+------+-------------+------+------+------------------------------------+
| Confusion (pairs disagreeing): stale_state<->not_enough_info 31   subjective<->ambiguous 18  |
| Human vs committee prompt A: alpha per reason [view]    prompt A vs B [view]                  |
| Labelers: L03 gold .86 | L07 gold .79 | L11 gold .58 PAUSED       [export agreement JSON]     |
+----------------------------------------------------------------------------------------------+
```

*(The numbers are placeholders that show the layout. They are not results.)*

### 6.5 Adjudication

```
+----------------------------------------------------------------------------------------------+
| Adjudicate  bbc_news/eval/73#q0   disagreements: stale_state                                  |
| STATE (spans: L-a green/solid, L-b outlined)                                                  |
|  ... between [1999 and 2001] it employed ...                                                  |
| reason              L-a   L-b   final                                                         |
| stale_state          x     .    [ ]        L-a note: "dates are old"                          |
| answerable           .     x    [x]        L-b note: -                                        |
| [save adjudication]  [save + promote to gold]  explanation: ______________________            |
+----------------------------------------------------------------------------------------------+
```

---

## 7. Agreement metrics and the QA workflow

### 7.1 Metrics

| metric | unit | use |
|---|---|---|
| Krippendorff's α, nominal, per reason (binary) | items × labelers; `null` = missing | **the E13 decision metric** (FR-37) |
| 95% bootstrap CI | resample items, 1,000× | decide "at the level of" with uncertainty, not on point values |
| prevalence, n positives | per reason | α is unstable at low prevalence (FR-42) |
| α "any abstain" | answerable vs not | sanity check of the overall `p` target |
| α set-valued (MASI) | the full reason set | an overall figure for the label scheme |
| human vs committee prompt A/B, A vs B | per reason | E13 step 2: committee reliability, and whether humans agree with it |
| span token-F1 / Jaccard per role and per triggering reason | word units | evidence quality. E07's AP is used where ≥3 labelers exist |
| gold accuracy per labeler per reason | gold probes | screening (FR-28, FR-30) |
| active time per item: median, p90 per labeler, per reason checked | ms | cost; the time each reason adds; spotting rushing |

### 7.2 Decision rule support

The app reports, but does not decide, the E13 rule ("keep a candidate only if its α is at the level of the established reasons"). The dashboard shows each candidate's α and CI against:
- the minimum, and
- the median

of the established reasons' α over the same items. The exact rule is §11 Q2. The dilution half of the rule ("the others don't lose") is mainly a training-side test (E13 step 3). The app supports a human-side version in two ways:
- the confusion panel (FR-41);
- batches with and without the candidates in `reason_set` (FR-33). Each established reason's α and prevalence are then compared across the two conditions.

### 7.3 QA workflow

1. **Pilot (owner):** label about 100 items. Revise the guideline (version bump). Label the same 100 again after at least a week to get intra-rater α (MVP, §10).
2. **Seed gold:** promote about 40 pilot items (at least 3 per reason, plus `answerable`) with explanations. Use 12 for the quiz and the rest as hidden probes.
3. **Onboard** each labeler: guideline → quiz (FR-27) → active.
4. **Label:** double-label blind batches. Hidden gold runs at 20% for the first 50 items, then 5%.
5. **Monitor daily:**
   - a labeler with low rolling gold accuracy is paused automatically (FR-30);
   - a labeler with outlying time (median active time < 5 s) is flagged for review;
   - a labeler with outlying reason prevalence (more than 3× the batch rate on any reason) is flagged for review.
6. **Adjudicate** disagreements, internal admins only. Adjudicated items can become gold. α is always computed on raw labels.
7. **Freeze:** close the batch. Export the agreement JSON (FR-48), annotations (FR-45) and training rows (FR-46) with a manifest. The E13 analysis recomputes α offline with the shared module (FR-40) and records the numbers in `experiments/v3/e13_*/REPORT.md`.

### 7.4 Sample size (estimate, to confirm)

- A per-reason α CI of about ±0.1 needs roughly 30+ positive judgements per reason, more at low prevalence.
- Since the generator targets candidate rows per reason (E13 step 1), a first round of **about 60 targeted items per reason plus about 200 untargeted items** (about 800 items × 2 labels = 1,600 judgements) is a reasonable pilot.
- At an assumed 25–40 s per item, that is roughly 11–18 labeler-hours.
- These are planning assumptions, not measurements. The app's time metric replaces them after the owner's pilot.

---

## 8. Licensing and access control

### 8.1 Tiers

These are taken from `RELEASE_POLICY.md` §2, with the `unverified` handling from `experiments/v3/release/source_permissions.json` ("until then treat as restricted for a permissive model"):

| item `permissions` | when | who may see it |
|---|---|---|
| `libre` | source class `libre`, and no Jev output attached | `public` and `internal` |
| `restricted` | source class `restricted` **or `unverified`** or unknown (NC, research-only, gated, or no licence traced: ANLI, MS MARCO, MultiRC, WikiQA, AG News, RACE, Yelp, QQP, GLUE mirrors, `bbc_news`, ...) | `internal` only |
| `jev` | libre source plus Jev output attached (a model answer, or committee labels from Jev) | `internal` only |
| `jev+restricted` | both | `internal` only |
| *never imported* | sources that are eval-only and bar training: LLM-AggreFact (CC-BY-ND, gated), HaluBench (NC) as human-label targets | import refuses them (FR-55) |

### 8.2 Requirements

- **FR-55 (MUST)** Import refuses rows whose source is on an `eval_only_no_training` list kept in the app config. Seed the list from `DATASET_INVENTORY.md` §2.2 and §2.7: LLM-AggreFact and HaluBench. Test: importing an `llm_aggrefact` row fails with a clear message.
- **FR-56 (MUST)** An item's tier is the **maximum** of its source tier and the tier of anything attached to it. Attaching a Jev answer to a libre item makes it `jev`. Tiers can only be lowered by re-import with an explicit owner flag, which is logged. Test: as stated.
- **FR-57 (MUST)** Enforcement is on the server, by query filter, in `next`, item fetch, history, quiz/gold selection, adjudication and exports. A batch's `tier_ceiling` can only narrow it. Test: FR-50 plus a fuzz test over every item endpoint with a `public` session.
- **FR-58 (MUST)** Gold and quiz items shown to `public` labelers must be `libre`. The app warns when a batch open to `public` labelers has fewer than 12 libre gold items. Test: the quiz for a `public` user draws only libre gold.
- **FR-59 (SHOULD)** Exports carry `permissions`, `source_license` and `text_included` per row. Human labels from the app are tagged as human-made, so a `libre` item labelled only by humans stays `libre` (`RELEASE_POLICY.md` §2: "Every label in the row comes from … humans"). Test: export fields are present.
- **FR-60 (SHOULD)** Labelers accept a short contributor agreement at first login: what their labels are used for, and the licence of contributed labels (§11 Q4). Restricted and Jev content must not be copied out (for internal labelers). The acceptance is stored with a version. Test: the app can't be used before acceptance.

Share-alike sources (SNLI, VitaminC, FEVER, BoolQ, ...) are `libre` and may be shown to labelers. The share-alike constraint affects only *redistribution* of text in the release, which the training export handles with `text_included` (`RELEASE_POLICY.md` §3.2).

---

## 9. Non-functional requirements

- **NFR-1 Performance.** `next` and submit respond in < 200 ms (p95) with 50k items and 10 concurrent labelers on midlife. The page is interactive in < 1 s on a laptop. States are ≤ 2,000 characters in the E09 pool (`DATASET_INVENTORY.md` §3.1). Longer states must render without a page-level scroll jump (the state pane scrolls).
- **NFR-2 Speed of labelling.** A typical `answerable` item takes **one keystroke plus Enter**. The UI never needs the mouse. Target median active time ≤ 20 s for answerable items, measured in the pilot.
- **NFR-3 Laptop-first.** Usable at 1366×768 and up without horizontal scroll. Recent Chrome, Firefox and Safari. No build step, and no external CDN needed at runtime, so it works on a LAN without internet.
- **NFR-4 Privacy and minimal identity.**
  - A labeler is a pseudonym (`L07`) plus a login name. Contact details are optional, sit in the owner-only `identity` table, and never appear in exports.
  - **No personal email address or name is hard-coded** anywhere in code, config or fixtures. The owner account is created by a CLI command at install.
  - Deleting a labeler's identity keeps their labels under the pseudonym.
  - Time tracking is disclosed in the contributor agreement.
- **NFR-5 Security.**
  - Passwords use argon2id (or bcrypt).
  - Session cookies are `HttpOnly`, `Secure` (when served over HTTPS) and `SameSite=Strict`.
  - CSRF tokens protect mutating requests.
  - Login is rate-limited.
  - Invites and sessions are stored hashed.
  - External access needs HTTPS (a reverse proxy or tunnel; §11 Q5). In `SINGLE_USER` mode the app binds to loopback only.
- **NFR-6 Durability.**
  - SQLite in WAL mode.
  - A nightly online backup (`sqlite3 .backup` equivalent from Python) to `outputs/e13_labeler/backups/`.
  - Nothing is hard-deleted (labels, versions, spans, gold): retire and revoke flags only (owner policy: keep all artifacts).
- **NFR-7 Reproducibility.**
  - Every annotation records the app version (git commit), guideline version and batch config.
  - The α module is shared with offline analysis (FR-40).
  - The export manifest records the DB checksum (FR-49).
- **NFR-8 Testing.** pytest covers:
  - every MUST FR's "Test";
  - blindness (no hidden field ever in a labeler payload);
  - tier enforcement (fuzz);
  - the assignment property test (FR-32);
  - α against the reference package.

  Tests run without the GPU or network.
- **NFR-9 Accessibility.** Span roles are distinguishable without colour (§6.1). All controls are reachable by keyboard and carry visible focus.
- **NFR-10 Operations.** One `run.sh` (uvicorn), configured through env vars or a TOML file. The app is stateless apart from the database, so restarting it loses only locks.

---

## 10. Milestones

### M1: MVP, owner only (target: about 2–3 days of build)

Scope:
- `SINGLE_USER=1`: one owner account on loopback (FR-54).
- CLI import of pool JSONL with tier resolution (FR-1–FR-7, FR-10, FR-11, FR-55, FR-56).
- The `reasons` task with all ten reasons plus `answerable`, the note, skip, and spans on text and JSON states, with word-snap and the four roles (FR-12–FR-20, FR-22). Keyboard map as §6.2.
- Model pseudo-labeler import (FR-9). With only one human, α comes from:
  1. owner vs committee prompts A and B;
  2. prompt A vs prompt B;
  3. owner vs owner (re-label after a gap, as a second "labeler" `L01-r2`).
- α per reason with CI (FR-37, FR-38 first bullet, FR-40), on a simple dashboard table.
- Exports: annotations, training rows and agreement JSON (FR-45, FR-46, FR-48, FR-49).
- Gold CRUD (FR-29), so the owner's pilot seeds gold for M2.

Exit criteria:
- the owner has labelled 100–200 pilot items;
- guideline v1 is written;
- at least 40 gold items exist;
- the time-per-item baseline is known;
- the α pipeline is checked against the reference package.

### M2: multi-labeler (target: about 3–4 days after M1)

Scope:
- invites, roles, clearance and revocation (FR-50–FR-53, FR-57, FR-58, FR-60);
- argon2 and the full NFR-5 security;
- the guideline gate, quiz and hidden gold with auto-pause (FR-26–FR-28, FR-30);
- batches, the overlap-aware queue, locks and progress (FR-31, FR-32, FR-35);
- edits and versions (FR-21);
- the full agreement dashboard with the confusion panel and labeler stats (FR-39, FR-41, FR-42);
- adjudication (FR-43) and flags (FR-44);
- HTTPS deployment for external labelers;
- backups (NFR-6).

Exit criteria: two or more labelers have finished one double-labelled batch, and per-reason α (with CI) is exported for E13.

### M3: extensions (as E13 needs them)

- the relation task type (FR-23, FR-24) and threshold stance for score questions (FR-25);
- the reason-set A/B for the human-side dilution test (FR-33);
- state-run serving (FR-36);
- `show_model_answer` audit batches (FR-34);
- MASI and unitized α (FR-38, FR-39).

---

## 11. Open questions for the owner

1. **Location and the old app.** Should `e13_labeler` live in this repo (`apps/e13_labeler/`, proposed) or be a new standalone repo like `nli-span-labeler`? Should `nli-span-labeler` be archived, or left alone?
2. **The keep rule.** Which reasons count as "established" for the comparison: all eight non-candidates, or only those with existing training signal (`unrelated`, `not_enough_info`, `conflicting_evidence`; `API_CONTRACT.md` §3)? And what is the rule? Options:
   - the candidate's α ≥ the lowest established α;
   - the candidate's α within 0.05 of the median;
   - the CI overlaps;
   - an absolute floor such as α ≥ 0.667 (Krippendorff's tentative-conclusions level).
3. **The reason list versus the semantics doc.** The output-semantics doc also lists **`out_of_scope`**, which is missing from the E13 list. Is it dropped, merged into `unrelated`, or added? Also confirm the renames: "insufficient" → `not_enough_info`, "non-factual framing" → `non_factual_support`.
4. **External labelers.** Who are they (friends or colleagues, a paid platform such as Prolific, or contractors)? Are they paid? Under what licence are their contributed labels released (CC-BY-4.0 or Apache-2.0, to match `RELEASE_POLICY.md`)? Does a contributor agreement need legal review?
5. **Hosting.** External access to midlife (192.168.1.100) needs a public HTTPS endpoint: a reverse proxy with a domain, a tunnel (for example Cloudflare Tunnel or Tailscale Funnel), or a small VPS with a copy of only the libre items. Which one is acceptable? The VPS option keeps restricted and Jev data off the public host entirely.
6. **Show the model answer?** Should blind batches ever show a model's answer? (Default: never.) If they do, from which teacher? A Jev answer makes the item internal-only.
7. **The reference time for `stale_state`.** Stale relative to what: today, an `asof` date per item (FR-7), or the question's implied time? Without a reference, `stale_state` may be unlabelable, and its α would then measure the definition, not the labelers.
8. **`false_premise` scope.** Is it judged only against the state (the evidence-relative reading in `API_CONTRACT.md` rule 10), or also against world knowledge? This decides whether a refuting span is required.
9. **Boundaries between `ambiguous`, `underspecified` and `subjective`.** These three are the most likely to blur. Should the guideline give a decision order (for example: several readings → `ambiguous`; one reading but a missing parameter → `underspecified`; one reading, every parameter given, still a matter of taste → `subjective`)?
10. **Overlap.** Two labelers per item (the minimum), or three on a subset? Three per item supports E07-style span AP and majority targets without ties.
11. **The human-side dilution test.** Run batches with and without the candidate checkboxes (FR-33) to see whether showing them changes how the established reasons are labelled? It costs about double the labels for that slice.
12. **Item granularity for multi-question rows.** Label one question per item (proposed, FR-2), or let a labeler do all questions of a state in one screen (faster, but answers can influence each other)?
13. **Restricted and unverified sources.** Is treating `unverified` as `restricted` (internal only) acceptable until the licences are traced? About 43% of E09 train rows come from such mirrors (`RELEASE_POLICY.md` §5).
14. **Retention.** How long are labeler identities (if any are recorded) kept after E13? Should the app support a labeler's request to have their labels removed, rather than only their identity?
