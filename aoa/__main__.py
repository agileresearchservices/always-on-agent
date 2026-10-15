"""`python -m aoa usage [--days N]`."""
from __future__ import annotations

import argparse

from . import usage_report


def main() -> None:
    ap = argparse.ArgumentParser(prog="aoa")
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("usage", help="estimated API spend per day, from the usage table")
    u.add_argument("--days", type=int, default=2, help="calendar days to include, today counted (default 2)")
    args = ap.parse_args()
    if args.cmd == "usage":
        print(usage_report.run(max(1, args.days)))


if __name__ == "__main__":
    main()
