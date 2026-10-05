#!/usr/bin/env python3
"""Build the E13 front-end test fixtures: a libre-only, stratified sample of the E09 eval pool.

    python3 docs/e13/fixtures/build_fixtures.py \
        [--pool /storage/ModernBERT-NLI-advanced/data/v3/e09/pool_eval.jsonl] \
        [--permissions /storage/ModernBERT-NLI-advanced/experiments/v3/release/source_permissions.json] \
        [--out docs/e13/fixtures/pool_eval_libre_sample.jsonl] [--seed 13] [--per-source 8] [--typed 40]

Standard library only, and deterministic: the same pool, permissions file and arguments give a byte-identical output.

Selection:
  * sources whose permissions class is exactly "libre"; "restricted" and "unverified" are dropped;
  * minus libre sources whose text carries third-party platform terms (EXCLUDE below, plus any licence note
    mentioning Reddit, Twitter or "per-task");
  * minus rows whose state matches the PII regexes (emails, phone numbers, card-like digit runs);
  * per source: forced edge cases first (longest state, most options, a noul question without criteria,
    JSON-looking text states, multi-question rows, both heldout values), then a seeded random fill.

Each output line is the pool line byte-for-byte, plus one field from the E13 import schema (§5.2):
`"permissions": "libre"`.  No teacher or Jev outputs are read or written.
"""
import argparse
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

MB = Path("/storage/ModernBERT-NLI-advanced")
DEFAULT_POOL = MB / "data/v3/e09/pool_eval.jsonl"
DEFAULT_PERMS = MB / "experiments/v3/release/source_permissions.json"
DEFAULT_OUT = Path(__file__).resolve().parent / "pool_eval_libre_sample.jsonl"

# Libre by licence, but the text itself comes from a platform with its own terms (or is in doubt).
EXCLUDE = {
    "go_emotions": "Reddit comments (licence note: 'Apache-2.0 (Reddit text)')",
    "fin_sentiment": "tweets (Twitter financial news; states contain t.co links); licence covers labels only",
    "hate_speech_scales": "social-media comments (YouTube, Reddit, Twitter, Gab) in Measuring Hate Speech",
    "prosocial_safety": "in doubt: ProsocialDialog contexts are seeded from Reddit-derived Social Chemistry",
}
THIRD_PARTY_RE = re.compile(r"reddit|twitter|per-task", re.I)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?!\d)")
CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
PII = {"email": EMAIL_RE, "phone": PHONE_RE, "card": CARD_RE}

LONG_STATE = 1500  # characters; pool states are capped at 2000


def pii_hits(text):
    return [k for k, rx in PII.items() if rx.search(text)]


def looks_json(s):
    return s.lstrip()[:1] in ("{", "[")


def parses_json(s):
    try:
        return isinstance(json.loads(s), (dict, list))
    except ValueError:
        return False


def n_options(q):
    if q["type"] == "noul":
        return 2
    return len(q.get("criteria") or ())


def features(r):
    qs = r["questions"].values()
    return {
        "long": len(r["state"]) >= LONG_STATE,
        "many_opts": max(n_options(q) for q in qs) >= 8,
        "noul_nocrit": any(q["type"] == "noul" and not q.get("criteria") for q in qs),
        "no_instr": any(not q.get("instructions") for q in qs),
        "jsonish_text": looks_json(r["state"]) and not parses_json(r["state"]),
        "json_state": parses_json(r["state"]),
        "multi_q": len(r["questions"]) > 1,
        "score": any(q["type"] == "score" for q in qs),
        "noul": any(q["type"] == "noul" for q in qs),
    }


def pick(rows, k, rng):
    """Forced edge cases first (one per feature present), then a seeded random fill; returns pool-order indexes."""
    chosen = []
    feats = [features(r) for _, r in rows]
    for f in ("long", "many_opts", "noul_nocrit", "no_instr", "jsonish_text", "json_state", "multi_q", "score",
              "noul"):
        cand = [i for i, ft in enumerate(feats) if ft[f] and i not in chosen]
        if cand:
            chosen.append(rng.choice(cand))
    for hv in (True, False):  # both heldout values when the source has both
        if not any(rows[i][1]["heldout"] is hv for i in chosen):
            cand = [i for i, (_, r) in enumerate(rows) if r["heldout"] is hv and i not in chosen]
            if cand:
                chosen.append(rng.choice(cand))
    rest = [i for i in range(len(rows)) if i not in chosen]
    rng.shuffle(rest)
    chosen += rest[: max(0, k - len(chosen))]
    return sorted(rows[i][0] for i in chosen)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pool", type=Path, default=DEFAULT_POOL)
    ap.add_argument("--permissions", type=Path, default=DEFAULT_PERMS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--per-source", type=int, default=8, help="rows per ordinary source (forced picks count)")
    ap.add_argument("--typed", type=int, default=40, help="rows from typed_decisions (the only noul/score source)")
    a = ap.parse_args()

    perms = json.loads(a.permissions.read_text())["sources"]
    libre = {s for s, v in perms.items() if v["class"] == "libre"}
    excluded = dict(EXCLUDE)
    for s in sorted(libre):
        if s not in excluded and THIRD_PARTY_RE.search(perms[s].get("licence", "")):
            excluded[s] = f"licence note mentions third-party terms: {perms[s]['licence']!r}"
    keep = libre - set(excluded)

    by_src = defaultdict(list)
    pii = Counter()
    seen = Counter()
    with open(a.pool) as f:  # streamed; the pool is ~15 MB
        for idx, line in enumerate(f):
            r = json.loads(line)
            s = r["source"]
            seen[s] += 1
            if s not in keep:
                continue
            hits = pii_hits(r["state"])
            if hits:
                for h in hits:
                    pii[(s, h)] += 1
                pii[(s, "rows")] += 1
                continue
            by_src[s].append((idx, r))

    rng = random.Random(a.seed)
    take = set()
    for s in sorted(by_src):
        take.update(pick(by_src[s], a.typed if s == "typed_decisions" else a.per_source, rng))

    out_lines = []
    with open(a.pool) as f:
        for idx, line in enumerate(f):
            if idx in take:
                # keep the pool row byte-for-byte and append the one import field before the closing brace
                raw = line.rstrip("\n").rstrip()
                assert raw.endswith("}") and "permissions" not in json.loads(raw)
                out_lines.append(raw[:-1] + ', "permissions": "libre"}')

    out_rows = [json.loads(l) for l in out_lines]
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w") as f:
        for l in out_lines:
            f.write(l + "\n")

    # summary (stderr, so the output file is the only artefact)
    src = Counter(r["source"] for r in out_rows)
    qtypes = Counter(q["type"] for r in out_rows for q in r["questions"].values())
    fts = Counter(k for r in out_rows for k, v in features(r).items() if v)
    log = lambda *x: print(*x, file=sys.stderr)  # noqa: E731
    log(f"wrote {len(out_rows)} rows, {sum(qtypes.values())} items -> {a.out}")
    log("question types:", dict(sorted(qtypes.items())))
    log("row features:", dict(sorted(fts.items())))
    log("per source:", dict(sorted(src.items())))
    log("libre sources absent from the pool:", sorted(keep - set(seen)))
    log("excluded libre sources:", json.dumps(excluded, indent=1))
    log("PII drops (eligible rows):", sum(v for (s, k), v in pii.items() if k == "rows"),
        {f"{s}/{k}": v for (s, k), v in sorted(pii.items())})


if __name__ == "__main__":
    main()
