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
    args = ap.parse_args()
    if args.cmd == "usage":
        print(usage_report.run(max(1, args.days)))
    elif args.cmd == "trace":
        print(flag.render_trace(Store(Settings.from_env().db_path), args.conv, as_json=args.json, max_chars=0 if args.json else 2000))
    elif args.cmd == "flags":
        s = Settings.from_env()
        store = Store(s.db_path)
        if args.scan:
            print(f"{len(flag.scan(store, s))} new")
        print(flag.format_flags(store.flags_list(None if args.all else "open")))


if __name__ == "__main__":
    main()
