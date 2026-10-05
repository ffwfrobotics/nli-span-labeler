# E13 test fixtures: libre-only sample of the E09 eval pool

`pool_eval_libre_sample.jsonl` is for building and testing the E13 labelling app front end and import code
(`../E13_LABELING_APP_REQUIREMENTS.md`). It holds **384 rows, which give 695 items** (one item per (row, qid), FR-2),
drawn from ModernBERT-NLI-Advanced `data/v3/e09/pool_eval.jsonl`.

- **Licensing:** only sources classed `libre` in `../source_permissions.json` are used, minus four libre sources
  whose text carries third-party platform terms. See `SOURCES.md` for per-source counts, licences, the share-alike
  rows and the exclusions.
- **Faithful format:** each line is the pool line **byte-for-byte**, with exactly one field appended: `"permissions":
  "libre"`. This is the optional import field from spec §5.2 (FR-6 rule 1). No other field is added. In
  particular, `source_license`, which is an item column (§5.1) and an export field (§5.5) but not an import field,
  is left out: the importer should look it up from `source_permissions.json`. There is no `e13` and no
  `model_answers`.
- **No teacher output:** no Jev answers, no committee or teacher outputs, no labels beyond the pool's own `gold`.
- **Not a training or eval set.** It is a small, edge-case-heavy sample, so don't report numbers on it.

## Contents

| | count |
|---|---:|
| rows / items | 384 / 695 |
| items by type | choice 556 · noul 59 (19 without criteria) · score 80 |
| sources | 44, 8 rows each, except `typed_decisions` with 40 (the only source with noul, score and JSON states) |
| JSON states (`typed_decisions`) | 40 rows, from 4 domains: agent_trace_observability 12, invoice_processing 12, customer_service 9, security_incidents 7 |
| text states that *look* like JSON but don't parse (FR-4 trap) | 7 (`hellaswag` `[header] ...`, `x_mnli_mm` `[I]mmigrants ...`) |
| long states (≥ 1500 chars; the pool caps states at 2000) | 36 |
| rows with a question of ≥ 8 options | 82 (the maximum is 10) |
| multi-question rows | 101 (the most is 22 questions, on `x_vitaminc/vitaminc:243`) |
| `heldout` true / false | 72 / 312 |

**Missing `instructions`:** the spec lists this as an edge case (FR-3), but **no pool row has it**, so this file
has none either. Test that path with a hand-made row (delete `instructions` from a copy of any row).

**PII:** before sampling, eligible rows whose `state` matches a regex for an email address, a phone number or a
card-like 13–19 digit run were dropped: 50 rows (`typed_decisions` 21, mostly synthetic addresses, `hermes_tools`
12, `bias_in_bios` 7, `mind2web` 6, and 1 each from `glaive_tools`, `hh_rlhf`, `reward_bench` and `toolace`). The
scan is a sanity check, not a PII guarantee, and covers only the state, not question text.

## Regenerating

Use the standard library only. Paths default to the ModernBERT-NLI-Advanced locations on midlife:

```bash
python3 docs/e13/fixtures/build_fixtures.py                       # writes docs/e13/fixtures/pool_eval_libre_sample.jsonl
python3 docs/e13/fixtures/build_fixtures.py \
    --pool /path/to/pool_eval.jsonl --permissions /path/to/source_permissions.json \
    --out /tmp/sample.jsonl --seed 13 --per-source 8 --typed 40
```

The build is deterministic: the same inputs and arguments give a byte-identical file
(sha256 `7a1f7826435d70ba0cd442a39e7f34f7d528c65f8d1bbcb6283a6b29c6ca0172` for this commit). A summary goes to
stderr. Per source, the builder first forces one row for each edge case present (long state, ≥ 8 options, noul without
criteria, missing instructions, JSON-looking text, JSON state, multi-question, score, noul, both `heldout`
values), then fills at random with `random.Random(seed)`. If `source_permissions.json` is reclassified, a re-run
follows it, so the counts above will change.

## Row fields

```json
{"id": "typed_decisions/agent_trace_observability_000033", "source": "typed_decisions", "split": "eval",
 "heldout": false, "state": "{\"trace\": ...}",
 "questions": {"needs_review": {"type": "noul", "instructions": "This trace requires human review.",
                                "criteria": {"false": "No human attention is warranted.", "true": "A human should inspect this run."}},
               "risk": {"type": "score", "instructions": "How risky was the agent's behaviour in this trace?",
                        "criteria": ["Benign: ...", "Low: ...", "Moderate: ...", "High: ..."]}, "...": {}},
 "gold": {"needs_review": true, "risk": 2.7, "...": "..."},
 "permissions": "libre"}
```

| field | type | notes |
|---|---|---|
| `id` | string | Row id, unique in the file, of the form `<source>/<split-or-domain>/<n>` or `<source>/<name>`. Treat it as opaque (FR-2 builds `item_id = "<id>#<qid>"`). It contains `/` and `:` (`x_snli/snli:246`), so URL-encode it in routes. |
| `source` | string | The task or dataset name. It keys `source_permissions.json`, but is **not shown to labelers** (§5.3). |
| `split` | string | Always `"eval"` here. |
| `heldout` | bool | `true` means the task was not in the decider teacher's training mixture (E09). Metadata only; hide it from labelers. |
| `state` | string | **Always a string.** For `typed_decisions` it is a JSON-encoded object (FR-4: detect by parsing), otherwise plain text that can contain newlines, `[header]`-style brackets, URLs and non-ASCII. Store it exactly as given (FR-5): span offsets index this string. |
| `questions` | object | `{qid: question}`. Keys are arbitrary strings: `q0`, `q1`, ... for most sources, semantic names (`needs_review`, `risk`) for `typed_decisions`, and dataset ids (`snli_test_8206`, `mnli_validation_mismatched_3713`) for `x_*` rows, where each qid is one hypothesis about the same premise. Iterate in object order. |
| `questions[qid].type` | `"choice"` \| `"noul"` \| `"score"` | See the next section. |
| `questions[qid].instructions` | string | The question, or for `noul` the statement to judge. Present on every row here, but optional in the format (fall back to the qid). |
| `questions[qid].criteria` | object \| array \| absent | Type-specific; see below. |
| `gold` | object | `{qid: answer}`, with one entry per qid in every row of this file. `choice` gives an option key (string), `noul` a bool, and `score` a **float** level in `[0, n-1]` (for example `2.7`), not an int. It is hidden from labelers (§5.3). For `typed_decisions` and `synth` it is teacher-made, not human (`DATASET_INVENTORY.md`). |
| `permissions` | `"libre"` | **Added by the builder** (the §5.2 import field). Not in the pool. |

## How each question type maps to options

This follows `option_texts()`/`options()`/`gold_index()` in ModernBERT-NLI-Advanced
`experiments/v3/e09_jev_distill/train_student.py` and `experiments/v3/e08_arc2_pilot/analyze.py`.

| type | option keys (what an answer or span `option` refers to) | option text shown | gold → key |
|---|---|---|---|
| `choice` | the keys of `criteria` (a dict, in its order): `"business"`, `"entailment"`, ... | `key` with `_` → space if the description equals the key, otherwise `"key: description"` | `gold` is the key itself |
| `noul` | always `["true", "false"]`, in that order | `"true: <criteria.true>"`, `"false: <criteria.false>"`. If `criteria` is absent (19 items here), use the defaults `true: The statement holds.` and `false: The statement does not hold.` | `true` → `"true"`, `false` → `"false"` |
| `score` | **`"0"`, `"1"`, ..., `"n-1"`**, where `n = len(criteria)` and `criteria` is a **list** | `criteria[i]` (these already read `"Low: ..."`), shown as level `i` | `str(round(gold))`, for example `2.7` → `"3"`. Python's `round` rounds halves to even (`2.5` → `"2"`) |

Choice keys can be whole sentences (`piqa`, `hellaswag`, `winogrande`, `xstory_cloze` use the answer text as both
key and description), so don't assume short slugs. Score levels are keyed by their index string, not by the
criterion text. This matches the `option: "3"` and `stance: {"0": ...}` examples in §5.4.

## Hedging: what is stable, and what may change

The front end can start now, but **the row schema is not frozen.** Expect fix requests as experiments land.

**Safe to build against (unlikely to change):**
- the envelope `id`, `source`, `split`, `heldout`, `state`, `questions`, `gold`, and one item per (row, qid);
- the three question types and their option keys (dict keys / `true,false` / `"0".."n-1"`);
- `state` as a string, with JSON states as JSON-encoded strings;
- spans indexing the exact `state` string (API_CONTRACT rule 7).

**Likely to change, or not there yet:**
1. **The schema freeze is pending.** Roadmap item A2.1 (freezing the row/answer schema) waits on decision D8
   (stance bottleneck and relevance → abstention). Field names around relation and abstention may still move.
2. **Relation, abstention-reason and evidence fields don't exist in pool rows yet** (A2.4). The §5.4/§5.5
   `relation`, `reasons`, `spans` and `targets` shapes are spec-only. Nothing in this file exercises them, and
   when they arrive they may be added to the import row.
3. **The abstain-reason list is provisional.** `stale_state` and `subjective` are *candidates* (§1.4) and may be
   dropped after E13. Drive the checkbox list from the batch's `reason_set` (§5.3), not a hard-coded list of 10.
4. **Per-row permission tags are a QA-audit gate.** They are not in the pool yet. `permissions` here was added by
   this builder from the source class. Real imports will mostly lack it and fall back to `source_permissions.json`
   (FR-6), and the classes themselves are still being audited: PIQA moved to `libre` on 2026-10-05, though spec
   §8.1's example list still names it as restricted.
5. **Gold can be absent.** Every row here has gold for every qid, but the format makes it optional, and E13-generated
   candidate rows may have none. Handle `gold` missing, `{}`, or missing a qid. `score` gold is fractional.
6. **Option rendering of dict/list criteria may change.** Every `criteria` value here is a plain string (choice/noul)
   or a list of strings (score). If a value is a dict or list, render it as canonical/pretty JSON, not a Python repr
   (FR-3, `API_CONTRACT.md` §4). The exact canonical form (key order, spacing) is not settled.
7. **The noul default descriptions** (`The statement holds.` / `The statement does not hold.`) come from the
   E09 student code, not from a contract, and could be reworded.
8. **The sample is not stable across permission changes.** Don't hard-code row ids from it in tests if the
   permissions file might be updated. Use features (for example "the first `noul` item without criteria").

## Quick start

```python
import json

NOUL_DEFAULT = {"true": "The statement holds.", "false": "The statement does not hold."}

def options(q):
    """[(key, text)] in display order."""
    if q["type"] == "noul":
        cr = q.get("criteria") or NOUL_DEFAULT
        return [(k, f"{k}: {cr.get(k, NOUL_DEFAULT[k])}") for k in ("true", "false")]
    if q["type"] == "score":
        return [(str(i), c) for i, c in enumerate(q["criteria"])]
    return [(k, k.replace("_", " ") if (d or k) == k else f"{k.replace('_', ' ')}: {d}")
            for k, d in q["criteria"].items()]

def state_format(s):
    try:
        return "json" if isinstance(json.loads(s), (dict, list)) else "text"
    except ValueError:
        return "text"

with open("docs/e13/fixtures/pool_eval_libre_sample.jsonl") as f:
    for line in f:
        row = json.loads(line)
        for qid, q in row["questions"].items():
            item_id = f"{row['id']}#{qid}"
            prompt = q.get("instructions") or qid
            opts = options(q)
            gold = row.get("gold", {}).get(qid)          # may be None
            print(item_id, row["permissions"], state_format(row["state"]), q["type"], len(opts), prompt[:60])
```
