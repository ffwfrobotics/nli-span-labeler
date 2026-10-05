"""
Command-line entry point.

    python -m e13_labeler init-db
    python -m e13_labeler create-owner [--login NAME]
    python -m e13_labeler serve [--host 127.0.0.1] [--port 8000] [--reload]
"""

import argparse
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

    p = sub.add_parser("serve", help="run the web app")
    p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
