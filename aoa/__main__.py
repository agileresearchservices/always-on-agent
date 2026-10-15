"""`python -m aoa usage [--days N]`, `trace <conv>`, `flags [--scan]`."""
from __future__ import annotations

import argparse

from . import flag, usage_report
from .config import Settings
from .store import Store


def main() -> None:
    ap = argparse.ArgumentParser(prog="aoa")
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("usage", help="estimated API spend per day, from the usage table")
    u.add_argument("--days", type=int, default=2, help="calendar days to include, today counted (default 2)")
    t = sub.add_parser("trace", help="print a conversation's messages (the stable trace export)")
    t.add_argument("conv", help="conversation id, e.g. the session name")
    t.add_argument("--json", action="store_true", help="machine-readable export")
    f = sub.add_parser("flags", help="list flagged bad outcomes")
    f.add_argument("--scan", action="store_true", help="look for new ones first")
    f.add_argument("--all", action="store_true", help="include flags that are no longer open")
    pr = sub.add_parser("propose", help="draft and validate eval cases for open flags (costs model calls and eval runs)")
    pr.add_argument("--limit", type=int, default=1, help="how many open flags to propose for (default 1)")
    pr.add_argument("--session", default="main")
    sub.add_parser("proposals", help="list eval-case proposals and their status")
    args = ap.parse_args()
    if args.cmd == "usage":
        print(usage_report.run(max(1, args.days)))
    elif args.cmd == "trace":
        print(flag.render_trace(Store(Settings.from_env().db_path), args.conv, as_json=args.json, max_chars=0 if args.json else 2000))
    elif args.cmd == "propose":
        import asyncio

        from . import eval_proposal

        s = Settings.from_env()
        store = Store(s.db_path)
        flag.scan(store, s)
        todo = [f for f in store.flags_list("open") if store.proposal_find(f["conv_id"], f["reason"]) is None][: max(1, args.limit)]
        ids = asyncio.run(eval_proposal.start_proposals(s, args.session, todo))
        print(f"started {len(ids)} proposal workflow(s); cards arrive in Slack when ready" if ids else "nothing to propose")
    elif args.cmd == "proposals":
        for p in Store(Settings.from_env().db_path).proposals_list():
            print(f"{p['id']}  {p['status']:14} {p['conv_id']}  {p['reason']}")
    elif args.cmd == "flags":
        s = Settings.from_env()
        store = Store(s.db_path)
        if args.scan:
            print(f"{len(flag.scan(store, s))} new")
        print(flag.format_flags(store.flags_list(None if args.all else "open")))


if __name__ == "__main__":
    main()
