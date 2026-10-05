"""
Command-line entry point.

    python -m e13_labeler init-db
    python -m e13_labeler create-owner [--login NAME]
    python -m e13_labeler import FILE [--batch NAME] [--replace] [--allow-lower-tier]
    python -m e13_labeler batch {list | open|close|draft NAME | config NAME ... | relabel NAME NEW ...}
    python -m e13_labeler import-labels FILE                       # model pseudo-labelers (FR-9)
    python -m e13_labeler export [--kind ...] [--batch ...] [--permissions ...] [--no-text] ...
    python -m e13_labeler agreement [--batch ...]
    python -m e13_labeler gold {list | import FILE | promote ITEM --from L01 | retire ITEM}
    python -m e13_labeler serve [--host 127.0.0.1] [--port 8000] [--reload]
"""

import argparse
import json
import getpass
import os
import sys

from . import config
from .db import audit, get_db, init_db


def cmd_init_db(args) -> int:
    version = init_db()
    print(f"Database {config.db_path()} at schema version {version}")
    return 0


def cmd_create_owner(args) -> int:
    """NFR-4: the owner account is created here, never hard-coded."""
    from .auth import create_labeler, get_owner

    init_db()
    login = args.login or input("Owner login name: ").strip()
    if not login:
        print("A login name is required", file=sys.stderr)
        return 2
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat password: "):
            print("Passwords differ", file=sys.stderr)
            return 2
    if len(password) < 10:
        print("Use at least 10 characters", file=sys.stderr)
        return 2
    with get_db() as conn:
        if get_owner(conn):
            print("An owner already exists", file=sys.stderr)
            return 1
        owner = create_labeler(conn, login, password, role="owner", clearance="internal", status="active")
        audit(conn, owner["id"], "create_owner", owner["pseudonym"])
    print(f"Created owner {owner['pseudonym']} ({login})")
    return 0


def cmd_import(args) -> int:
    """FR-11: import pool JSONL (requirements §5.2)."""
    import json

    from .importer import import_file

    init_db()
    with get_db() as conn:
        report = import_file(conn, args.file, batch=args.batch, replace=args.replace,
                             allow_lower_tier=args.allow_lower_tier)
    for lineno, message in report.errors:
        print(f"{args.file}:{lineno}: rejected: {message}", file=sys.stderr)
    summary = {k: v for k, v in report.as_dict().items() if k != "errors"}
    print(json.dumps(summary))
    return 1 if report.n_rejected else 0


def cmd_batch(args) -> int:
    """Batches (FR-31): list, open/close, configure overlap and the reliability subset, re-label."""
    from . import batches

    init_db()
    try:
        with get_db() as conn:
            if args.action == "list":
                for b in conn.execute("SELECT * FROM batches ORDER BY id").fetchall():
                    d = batches.describe(conn, b)
                    extra = (f"relabel_of={d['relabel_of']} after={d['relabel_after_days']}d" if d["relabel_of"]
                             else f"overlap={d['overlap_target']} subset={d['reliability_subset']}"
                                  f"@{d['reliability_overlap']}")
                    print(f"{d['name']}\t{d['status']}\titems={d['n_items']}\t{extra}\tceiling={d['tier_ceiling']}")
                return 0
            if args.action in ("open", "close", "draft"):
                status = {"close": "closed"}.get(args.action, args.action)
                for warning in batches.set_status(conn, args.name, status):
                    print(f"note: {warning}", file=sys.stderr)
                print(f"{args.name}: {status}")
            elif args.action == "config":
                result = batches.configure(
                    conn, args.name, overlap_target=args.overlap, reliability_fraction=args.reliability,
                    reliability_overlap=args.reliability_overlap, priority=args.priority,
                    tier_ceiling=args.tier_ceiling, require_note=args.require_note,
                    relabel_after_days=args.after_days)
                print(json.dumps(result))
            elif args.action == "relabel":
                result = batches.create_relabel(conn, args.name, args.new_name, fraction=args.fraction,
                                                after_days=args.after_days if args.after_days is not None else 7)
                print(json.dumps(result))
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def _filters(args, **extra):
    from .records import Filters

    return Filters(batches=args.batch or (), permissions=args.permissions or (), since=args.since,
                   until=args.until, include_models=not args.no_models, include_gold=args.include_gold, **extra)


def cmd_export(args) -> int:
    """FR-45 to FR-49: write an export run under outputs/e13_labeler/exports/<timestamp>/."""
    from .exports import write_export

    init_db()
    try:
        with get_db() as conn:
            manifest = write_export(conn, kinds=args.kind or ("annotations", "training", "agreement", "items"),
                                    filters=_filters(args), out_root=args.out, text_included=not args.no_text,
                                    n_boot=args.n_boot, seed=args.seed)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    print(json.dumps({"directory": manifest["directory"], "files": manifest["files"]}, indent=1))
    return 0


def _fmt(x, digits=3):
    return "–" if x is None else f"{x:.{digits}f}"


def print_agreement(rep: dict) -> None:
    def table(title, section):
        print(f"\n{title}")
        print(f"  {'reason':22} {'n':>5} {'alpha':>7} {'CI95':>15} {'prev':>6}  note")
        rows = list(section["per_reason"].items()) + [("any abstain", section["any_abstain"])]
        for reason, d in rows:
            ci = "–" if not d["ci95"] else f"{d['ci95'][0]:.2f}..{d['ci95'][1]:.2f}"
            note = "unstable" if d["unstable"] and d["n_items"] else ""
            print(f"  {reason:22} {d['n_items']:>5} {_fmt(d['alpha']):>7} {ci:>15} "
                  f"{_fmt(d['prevalence'], 2):>6}  {note}")

    inter = rep["inter_rater"]
    table(f"Inter-rater α ({inter['n_items_pairable']} items with 2+ human labels; labelers: "
          f"{', '.join(inter['labelers']) or 'none'})", inter)
    for c, d in inter["candidates"].items():
        print(f"  candidate {c}: α {_fmt(d['alpha'])} vs established min {_fmt(d['min'])} / "
              f"median {_fmt(d['median'])}")
    if rep["intra_rater"]["n_pairs"]:
        table(f"Intra-rater α (re-label pairs: {rep['intra_rater']['n_pairs']}; consistency, not agreement)",
              rep["intra_rater"])
    for m, d in rep["human_vs_model"].items():
        table(f"Human vs {m} ({d['n_pairs']} pairs)", d)
    for pair, d in rep["model_vs_model"].items():
        table(f"{pair.replace('|', ' vs ')} ({d['n_pairs']} items)", d)


def cmd_agreement(args) -> int:
    """FR-37/38: the agreement report in the terminal."""
    from .analysis import agreement_record, report
    from .records import load_annotations

    init_db()
    with get_db() as conn:
        records = [agreement_record(r) for r in load_annotations(conn, _filters(args))]
    rep = report(records, n_boot=args.n_boot, seed=args.seed)
    if args.json:
        print(json.dumps(rep, indent=1))
    else:
        print_agreement(rep)
    return 0


def cmd_import_labels(args) -> int:
    """FR-9: committee or teacher labels as model pseudo-labelers."""
    from .model_labels import import_model_labels

    init_db()
    with get_db() as conn:
        report = import_model_labels(conn, open(args.file, encoding="utf-8"), args.file)
    for lineno, message in report.errors:
        print(f"{args.file}:{lineno}: rejected: {message}", file=sys.stderr)
    print(json.dumps({k: v for k, v in report.as_dict().items() if k != "errors"}))
    return 1 if report.n_rejected else 0


def cmd_gold(args) -> int:
    """FR-29: gold items."""
    from . import gold
    from .labelling import SubmissionError

    init_db()
    try:
        with get_db() as conn:
            if args.action == "list":
                for g in gold.list_gold(conn, include_retired=args.all):
                    answer = "answerable" if g["answerable"] else ", ".join(g["reasons"])
                    retired = "\tretired" if g["retired"] else ""
                    print(f"{g['item_id']}\t{answer}\t{len(g['spans'])} spans{retired}")
            elif args.action == "import":
                n = 0
                for line in open(args.file, encoding="utf-8"):
                    if line.strip():
                        row = json.loads(line)
                        gold.save_gold(conn, row["item_id"], answerable=bool(row.get("answerable")),
                                       reasons=row.get("reasons") or [], spans=row.get("spans") or [],
                                       alternatives=row.get("alternatives"), explanation=row.get("explanation"))
                        n += 1
                print(f"saved {n} gold items")
            elif args.action == "promote":
                g = gold.promote(conn, args.item, args.labeler, explanation=args.explanation)
                print(json.dumps(g))
            elif args.action == "retire":
                gold.retire_gold(conn, args.item)
                print(f"retired {args.item}")
    except (ValueError, SubmissionError) as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    host = args.host
    if config.single_user() and host not in config.LOOPBACK_HOSTS:
        print("SINGLE_USER=1 binds to loopback only (FR-54); using 127.0.0.1", file=sys.stderr)
        host = "127.0.0.1"
    if not config.single_user() and not config.cookie_secure():
        print("warning: COOKIE_SECURE=0 outside SINGLE_USER mode; only for plain-HTTP testing on a trusted LAN",
              file=sys.stderr)
    # Proxy trust lives in one place (auth.client_ip with TRUSTED_PROXIES). uvicorn's own
    # proxy_headers would otherwise believe X-Forwarded-For from 127.0.0.1 by default.
    uvicorn.run("e13_labeler.app:app", host=host, port=args.port, reload=args.reload,
                proxy_headers=False, forwarded_allow_ips="")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m e13_labeler", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create or migrate the database").set_defaults(func=cmd_init_db)

    p = sub.add_parser("create-owner", help="create the owner account (once)")
    p.add_argument("--login")
    p.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    p.set_defaults(func=cmd_create_owner)

    p = sub.add_parser("import", help="import pool JSONL rows as items")
    p.add_argument("file")
    p.add_argument("--batch", help="add the imported items to this batch (created as a draft if new)")
    p.add_argument("--replace", action="store_true", help="overwrite items whose state changed")
    p.add_argument("--allow-lower-tier", action="store_true",
                   help="owner only: let a replacement lower an item's permissions tier (logged)")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("batch", help="list, open/close, configure or re-label batches")
    bsub = p.add_subparsers(dest="action", required=True)
    bsub.add_parser("list", help="list batches")
    for action in ("open", "close", "draft"):
        bsub.add_parser(action, help=f"{action} a batch").add_argument("name")
    c = bsub.add_parser("config", help="set overlap, reliability subset, priority, ...")
    c.add_argument("name")
    c.add_argument("--overlap", type=int, help="labelers per item (1..n; 3 is ideal)")
    c.add_argument("--reliability", type=float, help="share of items in the reliability subset (0..1)")
    c.add_argument("--reliability-overlap", type=int, help="labelers per item in the subset (>= 2)")
    c.add_argument("--priority", type=int)
    c.add_argument("--tier-ceiling")
    c.add_argument("--require-note", action=argparse.BooleanOptionalAction, default=None)
    c.add_argument("--after-days", type=int, help="re-label batches: minimum gap in days")
    r = bsub.add_parser("relabel", help="create an intra-rater re-label batch over a batch")
    r.add_argument("name", help="source batch")
    r.add_argument("new_name", help="name of the re-label batch")
    r.add_argument("--fraction", type=float, help="sample of the source (default: its subset, else all)")
    r.add_argument("--after-days", type=int, help="minimum gap before an item comes back (default 7)")
    p.set_defaults(func=cmd_batch)

    def add_filters(p):
        p.add_argument("--batch", action="append", help="only these batches (repeatable)")
        p.add_argument("--permissions", action="append", help="only these release tiers (repeatable; FR-47)")
        p.add_argument("--since", help="annotations created at or after (YYYY-MM-DD)")
        p.add_argument("--until", help="annotations created before (YYYY-MM-DD)")
        p.add_argument("--no-models", action="store_true", help="leave out model pseudo-labelers")
        p.add_argument("--include-gold", action="store_true", help="include gold items and gold probes")
        p.add_argument("--n-boot", type=int, default=1000, help="bootstrap resamples for α CIs")
        p.add_argument("--seed", type=int, default=0)

    p = sub.add_parser("export", help="write annotation / training / agreement / items exports (FR-45..49)")
    p.add_argument("--kind", action="append", choices=["annotations", "training", "agreement", "items"],
                   help="what to write (repeatable; default all)")
    p.add_argument("--no-text", action="store_true", help="training rows without state/question text (§5.5)")
    p.add_argument("--out", help="export root (default outputs/e13_labeler/exports)")
    add_filters(p)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("agreement", help="print per-reason α (FR-37/38)")
    p.add_argument("--json", action="store_true")
    add_filters(p)
    p.set_defaults(func=cmd_agreement)

    p = sub.add_parser("import-labels", help="import model pseudo-labeler annotations (FR-9)")
    p.add_argument("file")
    p.set_defaults(func=cmd_import_labels)

    p = sub.add_parser("gold", help="gold items (FR-29)")
    gsub = p.add_subparsers(dest="action", required=True)
    g = gsub.add_parser("list")
    g.add_argument("--all", action="store_true", help="include retired gold")
    g = gsub.add_parser("import", help="JSONL of {item_id, answerable|reasons, spans, alternatives, explanation}")
    g.add_argument("file")
    g = gsub.add_parser("promote", help="gold from a labeler's annotation of the item")
    g.add_argument("item")
    g.add_argument("--from", dest="labeler", required=True, help="labeler pseudonym, e.g. L01")
    g.add_argument("--explanation")
    g = gsub.add_parser("retire")
    g.add_argument("item")
    p.set_defaults(func=cmd_gold)

    p = sub.add_parser("serve", help="run the web app")
    p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
