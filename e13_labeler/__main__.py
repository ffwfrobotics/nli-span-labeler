"""
Command-line entry point.

    python -m e13_labeler init-db
    python -m e13_labeler create-owner [--login NAME]
    python -m e13_labeler import FILE [--batch NAME] [--replace] [--allow-lower-tier]
    python -m e13_labeler batch {list | open|close|draft NAME | config NAME ... | relabel NAME NEW ...}
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


def cmd_serve(args) -> int:
    import uvicorn

    host = args.host
    if config.single_user() and host not in config.LOOPBACK_HOSTS:
        print("SINGLE_USER=1 binds to loopback only (FR-54); using 127.0.0.1", file=sys.stderr)
        host = "127.0.0.1"
    uvicorn.run("e13_labeler.app:app", host=host, port=args.port, reload=args.reload)
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

    p = sub.add_parser("serve", help="run the web app")
    p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
