#!/usr/bin/env python3
"""
Re-checker
==========

Re-tests the proxies saved in working_proxies.txt (or any file you pass) to see
which are STILL working right now. Free proxies die over the course of a day,
so a list that had 20 alive this morning may have fewer by afternoon.

It reuses the EXACT same checking method as proxy_grabber.py
(check_all / check_proxy), so results are consistent.

Usage:
    python3 recheck.py                       # rechecks working_proxies.txt
    python3 recheck.py my_list.txt           # recheck a different file
    python3 recheck.py --workers 50 --timeout 8
    python3 recheck.py --save                 # overwrite file with survivors
"""

from __future__ import annotations

import argparse
import os
import sys

# Reuse the same data model, checker, and colors from the grabber.
from proxy_grabber import Proxy, check_all, c


def load_proxies(path: str) -> list[Proxy]:
    """Read 'ip:port' (one per line) into Proxy objects, de-duplicated."""
    if not os.path.exists(path):
        print(c(f"[RECHECK] File not found: {path}", "red"))
        sys.exit(1)

    seen: set[str] = set()
    proxies: list[Proxy] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Lines may be "ip:port" or "ip:port  protocol  https" — take the
            # first whitespace-separated token, plus optional protocol column.
            parts = line.split()
            addr = parts[0]
            proto = parts[1] if len(parts) > 1 and parts[1] in (
                "socks4", "socks5", "http", "https") else None
            # accept "scheme://ip:port" too
            if "://" in addr:
                scheme, addr = addr.split("://", 1)
                proto = proto or scheme.replace("h", "").replace("a", "")
            if ":" not in addr or addr in seen:
                continue
            ip, port = addr.rsplit(":", 1)
            seen.add(addr)
            # If the file doesn't record a type, try all common types so we
            # still check each proxy AS its correct protocol (accuracy).
            protocols = [proto] if proto else ["socks4", "socks5", "http"]
            try:
                proxies.append(Proxy(ip=ip, port=int(port), protocols=protocols))
            except ValueError:
                continue
    return proxies


def main() -> None:
    ap = argparse.ArgumentParser(description="Re-check a saved proxy list")
    ap.add_argument("file", nargs="?", default="working_proxies.txt",
                    help="Proxy list file (default working_proxies.txt)")
    ap.add_argument("--workers", type=int, default=100,
                    help="Parallel check workers (default 100)")
    ap.add_argument("--timeout", type=float, default=8.0,
                    help="Per-proxy timeout seconds (default 8)")
    ap.add_argument("--retries", type=int, default=2,
                    help="Retries before marking a proxy dead (default 2); "
                         "recovers flaky free proxies for more accurate results")
    ap.add_argument("--save", action="store_true",
                    help="Overwrite the file with only the still-working proxies")
    args = ap.parse_args()

    proxies = load_proxies(args.file)
    if not proxies:
        print(c(f"[RECHECK] No proxies found in {args.file}", "yellow"))
        sys.exit(0)

    print(c("[RECHECK] ", "cyan") +
          f"Loaded {c(str(len(proxies)), 'bold')} proxies from "
          f"{args.file}. Re-testing now ...\n")

    results = check_all(proxies, workers=args.workers, timeout=args.timeout,
                        retries=args.retries)

    still = sorted([p for p in results if p.alive],
                   key=lambda p: p.latency_ms or 9e9)
    gone = [p for p in results if not p.alive]

    print(c("=" * 64, "bold"))
    print(c(" RE-CHECK RESULTS", "bold"))
    print(c("=" * 64, "bold"))
    print(f" Checked          : {len(results)}")
    print(f" Still working    : {c(str(len(still)), 'green')}")
    print(f" Stopped working  : {c(str(len(gone)), 'red')}")
    print(c("=" * 64, "bold"))

    if still:
        print(c("\n STILL WORKING (fastest first)\n", "green"))
        print(f" {'#':<4}{'IP:PORT':<24}{'HTTPS':<7}{'LATENCY':<11}{'EXIT IP'}")
        print(" " + "-" * 60)
        for i, p in enumerate(still, 1):
            https = c("yes", "green") if p.secure else c("no", "yellow")
            https_cell = https + " " * (7 - (3 if p.secure else 2))
            print(f" {i:<4}{p.addr:<24}{https_cell}"
                  f"{str(p.latency_ms) + 'ms':<11}{p.exit_ip or ''}")

    if gone:
        print(c("\n NO LONGER WORKING\n", "red"))
        for p in gone:
            print(f"   {p.addr}")

    if args.save:
        with open(args.file, "w", encoding="utf-8") as fh:
            fh.write("# ip:port  protocol  https_secure\n")
            for p in still:
                fh.write(f"{p.addr}  {p.protocol}  "
                         f"{'https' if p.secure else 'http'}\n")
        print(c(f"\nUpdated {args.file}: kept {len(still)} working, "
                f"removed {len(gone)} dead.", "cyan"))


if __name__ == "__main__":
    main()
