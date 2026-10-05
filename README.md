# E13 labeler

A web app for collecting **blind, independent human labels of abstain reasons** for
(state, question) items, with the evidence spans that triggered them. It also
measures per-reason agreement with Krippendorff's α. It produces the human
sample for experiment E13 (abstain-reason label quality).

- Requirements: [`docs/e13/E13_LABELING_APP_REQUIREMENTS.md`](docs/e13/E13_LABELING_APP_REQUIREMENTS.md)
- Status and plan: [`ROADMAP.md`](ROADMAP.md)
- Lineage: this repo began as the *NLI span labeler* (premise/hypothesis span
  annotation; final state at commit `72c9bbb`). Its FastAPI backbone and styles
  were kept; see [`docs/e13/E13_CODEBASE_EVALUATION.md`](docs/e13/E13_CODEBASE_EVALUATION.md).

**Status:** milestones M1 (owner-only MVP) and M2 (multi-labeler) are built:
import, the labelling screen, batches (overlap 1..n, reliability subset,
re-label passes), model pseudo-labelers, gold, per-reason α and the exports;
then invites, the contributor agreement, the guideline and quiz, hidden gold
with auto-pause, edits, progress, adjudication, span agreement, CSRF, backups
and an HTTPS deployment. What is left is in `ROADMAP.md`.

## Setup

Needs Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run python -m e13_labeler create-owner     # once; prompts for login and password
./run.sh                                      # http://127.0.0.1:8000, API docs at /docs
```

For a single person on one machine, `SINGLE_USER=1 ./run.sh` logs the owner in
automatically and refuses anything that isn't from loopback.

## A pilot, end to end

```bash
uv run python -m e13_labeler import docs/e13/fixtures/pool_eval_libre_sample.jsonl --batch pilot
uv run python -m e13_labeler batch config pilot --overlap 1      # 3 is ideal; 1 works
uv run python -m e13_labeler batch open pilot
SINGLE_USER=1 ./run.sh                                          # label at http://127.0.0.1:8000
uv run python -m e13_labeler import-labels committee_a.jsonl     # model pseudo-labelers (§5.4 rows)
uv run python -m e13_labeler batch relabel pilot pilot-r2 --fraction 0.2   # comes back after 7 days
uv run python -m e13_labeler agreement                           # per-reason α in the terminal
uv run python -m e13_labeler gold promote ITEM_ID --from L01     # seed gold from your labels
uv run python -m e13_labeler export                              # outputs/e13_labeler/exports/<timestamp>/
```

## Bringing in other labelers (M2)

1. **Gold for the quiz.** The quiz needs at least 12 gold items: each reason at
   least once (both candidates included) plus two `answerable` ones. Public
   labelers only ever see libre gold, so they need 12 libre ones (opening a
   batch warns otherwise). Seed gold from the pilot (`gold promote`), from
   adjudication ("Save + promote to gold"), or with `gold import FILE`.
2. **Revise the guideline.** Edit `e13_labeler/guideline.json` (definitions and
   span rules come from the code) and bump its `version`. Annotations record it.
3. **Invite.** Admin tab → Labelers → Invite. The link is shown once, works
   once and expires after 7 days. Only the owner invites admins or gives
   internal clearance.
4. The new labeler opens the link, picks a login, accepts the contributor
   agreement, reads the guideline and takes the quiz. Passing makes them
   active; one retake is allowed, then the owner decides (Resume).
5. While they label, hidden gold runs at 20% for their first 50 items and 5%
   after. A rolling gold accuracy under 0.6 pauses them and sends them to a
   retraining quiz.
6. **Adjudicate** disagreements on the Admin tab (internal admins). Results are
   stored apart from the raw labels; α never changes.

Labelers press `e` to reopen one of their last 20 submissions; each edit is a
new version.

The export directory holds `annotations.jsonl` (§5.4), `training.jsonl` (§5.5),
`agreement.json` (every number plus its inputs), `items.jsonl` (the pool format,
which re-imports byte-identically) and `manifest.json`. The JSON Schemas are in
`e13_labeler/schemas/`.

## Configuration

Set environment variables. The full list is in [`e13_labeler/config.py`](e13_labeler/config.py). The main ones:

| variable | default | meaning |
|---|---|---|
| `E13_OUTPUTS` | `outputs/e13_labeler` | database, exports and backups |
| `E13_DB` | `$E13_OUTPUTS/e13.db` | SQLite database |
| `SINGLE_USER` | `0` | owner auto-login, loopback only |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | bind address for `run.sh` |
| `ALLOWED_HOSTS` | any (loopback in `SINGLE_USER`) | the `Host` names the app answers |
| `COOKIE_SECURE` | `1` (`0` in `SINGLE_USER`) | `0` only for plain-HTTP tests on a trusted LAN |
| `TRUSTED_PROXIES` | (none) | proxies whose `X-Forwarded-For` is believed |
| `LOCK_TIMEOUT_MINUTES` | `20` | item lock lifetime |
| `BACKUP_INTERVAL_HOURS` | `24` | online backup when the newest is older (`0`: off) |
| `E13_GOLD_RATE_NEW` / `E13_GOLD_RATE` | `0.20` / `0.05` | hidden gold share, first 50 items / after |
| `E13_GOLD_THRESHOLD` | `0.6` | rolling gold accuracy that pauses a labeler |
| `E13_QUIZ_SIZE` | `12` | quiz items |

## Deploying for external labelers

External labelers need HTTPS. `deploy/` holds a Containerfile and a compose
file that put the app behind Caddy, which gets a certificate automatically:
see [`docs/e13/DEPLOY.md`](docs/e13/DEPLOY.md).

## Tests

```bash
uv run pytest
```

The tests run offline, without a GPU.

## License

MIT
