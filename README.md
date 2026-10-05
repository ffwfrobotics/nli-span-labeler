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

**Status:** under construction. The backbone, schema and accounts are in place.
The labelling screen, import and α are milestone M1.

## Setup

Needs Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run python -m e13_labeler create-owner     # once; prompts for login and password
./run.sh                                      # http://127.0.0.1:8000, API docs at /docs
```

For a single person on one machine, `SINGLE_USER=1 ./run.sh` logs the owner in
automatically and refuses anything that isn't from loopback.

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
