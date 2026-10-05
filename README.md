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

**Status:** milestone M1 (owner-only MVP) is complete: import, the labelling
screen, batches (overlap 1..n, reliability subset, re-label passes), model
pseudo-labelers, gold, per-reason α and the exports. M2 (multi-labeler:
invites, quiz, hidden gold, adjudication) is next; see `ROADMAP.md`.

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

The export directory holds `annotations.jsonl` (§5.4), `training.jsonl` (§5.5),
`agreement.json` (every number plus its inputs), `items.jsonl` (the pool format,
which re-imports byte-identically) and `manifest.json`. The JSON Schemas are in
`e13_labeler/schemas/`.

## Configuration

Set environment variables. The full list is in [`e13_labeler/config.py`](e13_labeler/config.py). The main ones:

| variable | default | meaning |
|---|---|---|
| `E13_DB` | `outputs/e13_labeler/e13.db` | SQLite database |
| `SINGLE_USER` | `0` | owner auto-login, loopback only |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | bind address for `run.sh` |
| `LOCK_TIMEOUT_MINUTES` | `20` | item lock lifetime |
| `COOKIE_SECURE` | `0` | set to `1` behind HTTPS |
| `TRUSTED_PROXIES` | (none) | proxies whose `X-Forwarded-For` is believed |

## Tests

```bash
uv run pytest
```

The tests run offline, without a GPU.

## License

MIT
