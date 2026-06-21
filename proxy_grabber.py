from __future__ import annotations

import argparse
import concurrent.futures as cf
import sys
import threading
import time
from dataclasses import dataclass, field

import requests

# ---------------------------------------------------------------------------
# Configurationx
# ---------------------------------------------------------------------------

GEONODE_API = "https://proxylist.geonode.com/api/proxy-list"

# Filters matching the Geonode UI shown in the screenshot.
DEFAULT_FILTERS = {
    "country": "US",
    "anonymityLevel": "elite",
    "protocols": "socks4",
    "speed": "fast",
    "sort_by": "lastChecked",
    "sort_type": "desc",
}

# Liveness endpoints (HTTP): used to confirm a proxy actually forwards traffic
# and to read back the exit IP. Many free SOCKS4 proxies forward plain HTTP but
# cannot tunnel HTTPS, so HTTP is the reliable liveness test.
CHECK_URLS = [
    "http://api.ipify.org?format=json",
    "http://httpbin.org/ip",
    "http://ip-api.com/json",
]

# Security endpoint (HTTPS): used as a SEPARATE capability test. A proxy that
# passes this can carry TLS traffic end-to-end (it sees only encrypted bytes,
# cannot read or tamper) -> safe for real/sensitive use. HTTP-only proxies are
# still reported as alive, just flagged insecure for sensitive traffic.
HTTPS_CHECK_URL = "https://api.ipify.org?format=json"

# Hard cap on how much data we'll read from a proxy's response. A hostile
# proxy could try to flood us with a huge body; we only need a few bytes.
MAX_RESPONSE_BYTES = 4096

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# ANSI colors (auto-disabled if output is not a TTY).
_USE_COLOR = sys.stdout.isatty()


def c(text: str, color: str) -> str:
    if not _USE_COLOR:
        return text
    codes = {
        "green": "\033[92m",
        "red": "\033[91m",
        "yellow": "\033[93m",
        "cyan": "\033[96m",
        "bold": "\033[1m",
        "dim": "\033[2m",
    }
    return f"{codes.get(color, '')}{text}\033[0m"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Proxy:
    ip: str
    port: int
    # All protocols Geonode lists for this proxy (e.g. ["socks5"] or
    # ["http", "https"]). We check the proxy AS each of its real types.
    protocols: list[str] = field(default_factory=lambda: ["socks4"])
    country: str = "Unknown"
    # filled in during checking
    alive: bool = False
    secure: bool = False           # True if it can tunnel verified HTTPS/TLS
    working_protocol: str | None = None   # the type that actually connected
    latency_ms: float | None = None
    exit_ip: str | None = None
    error: str | None = None

    @property
    def protocol(self) -> str:
        """Primary/representative type (working one if known, else first)."""
        return self.working_protocol or self.protocols[0]

    @property
    def addr(self) -> str:
        return f"{self.ip}:{self.port}"

    @staticmethod
    def _scheme_for(proto: str) -> str:
        """Map a Geonode protocol to a requests/PySocks URL scheme.

        socks4h/socks5h resolve DNS through the proxy (more correct); http(s)
        proxies use the plain http scheme for both http and https targets.
        """
        proto = proto.lower()
        if proto == "socks5":
            return "socks5h"   # resolve DNS through the proxy
        if proto == "socks4":
            return "socks4a"   # socks4 remote-DNS variant (socks4h is invalid)
        return "http"   # http or https CONNECT proxy

    def proxies_dict(self, proto: str | None = None) -> dict:
        """Build the requests proxies mapping for a specific type."""
        proto = proto or self.protocol
        scheme = self._scheme_for(proto)
        url = f"{scheme}://{self.ip}:{self.port}"
        return {"http": url, "https": url}


# ---------------------------------------------------------------------------
# Step 1: GRAB
# ---------------------------------------------------------------------------

def _fetch_page(filters: dict, page: int, per_page: int) -> dict:
    """Fetch a single Geonode page; returns the parsed JSON (or {} on error)."""
    params = {**filters, "limit": per_page, "page": page}
    try:
        resp = requests.get(GEONODE_API, params=params,
                            headers=HEADERS, timeout=25)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        print(c(f"[GEONODE] page {page} failed: {exc}", "red"))
        return {}


def grab_proxies(limit: int = 0, filters: dict | None = None,
                 fetch_workers: int = 12) -> list[Proxy]:
    """Grab unique proxies FAST by fetching all pages concurrently.

    Speed tricks (verified against the live API):
      * per_page = 500 (the API's max) -> ~21 pages instead of ~107.
      * one request finds the total, then ALL remaining pages are fetched in
        parallel -> the full ~10k list lands in a few seconds, not minutes.
    limit=0 (default) grabs every available proxy. Duplicates never counted.
    """
    filters = filters if filters is not None else dict(DEFAULT_FILTERS)
    # Request only as many as needed: tiny limits pull a tiny page (so asking
    # for 20 fetches 20, not 500). 500 is the API hard max (600+ is rejected).
    per_page = min(500, limit) if limit else 500

    shown = ", ".join(f"{k}={v}" for k, v in filters.items()
                      if k not in ("sort_by", "sort_type")) or "none (ALL proxies)"
    print(c("[GEONODE] ", "cyan") + "Connecting to geonode.com free-proxy-list "
          f"(filters: {shown}) ...")

    t0 = time.perf_counter()

    # --- Request 1: page 1 also tells us the total count ---
    first = _fetch_page(filters, 1, per_page)
    total_available = first.get("total", 0)
    print(c("[GEONODE] ", "cyan") +
          f"Detected {c(str(total_available), 'bold')} total proxies "
          f"listed at Geonode for these filters.")

    import math
    pages_needed = max(1, math.ceil(total_available / per_page))
    if limit:
        pages_needed = min(pages_needed, math.ceil(limit / per_page))

    # --- Fetch every remaining page CONCURRENTLY ---
    all_rows: list[dict] = list(first.get("data", []))
    if pages_needed > 1:
        print(c("[GEONODE] ", "cyan") +
              f"Fetching {pages_needed} pages (500/page) with "
              f"{fetch_workers} parallel fetchers ...")
        with cf.ThreadPoolExecutor(max_workers=fetch_workers) as pool:
            futs = {pool.submit(_fetch_page, filters, pg, per_page): pg
                    for pg in range(2, pages_needed + 1)}
            for fut in cf.as_completed(futs):
                all_rows.extend(fut.result().get("data", []))

    # --- De-duplicate (repeats never counted) ---
    seen: set[str] = set()
    unique: list[Proxy] = []
    duplicates = 0
    for row in all_rows:
        addr = f"{row['ip']}:{row['port']}"
        if addr in seen:
            duplicates += 1
            continue
        seen.add(addr)
        protocols = [p.lower() for p in (row.get("protocols") or ["socks4"])]
        unique.append(Proxy(
            ip=row["ip"],
            port=int(row["port"]),
            protocols=protocols,
            country=row.get("country", "Unknown"),
        ))
        if limit and len(unique) >= limit:
            break

    elapsed = time.perf_counter() - t0
    print(c("[GEONODE] ", "cyan") +
          f"Grabbed {c(str(len(unique)), 'green')} unique proxies "
          f"({c(str(duplicates), 'yellow')} duplicates skipped) "
          f"in {c(f'{elapsed:.1f}s', 'bold')}.")

    # Type breakdown of everything fetched.
    type_counts: dict[str, int] = {}
    for p in unique:
        for proto in p.protocols:
            type_counts[proto] = type_counts.get(proto, 0) + 1
    breakdown = ", ".join(f"{c(str(n), 'bold')} {t}"
                          for t, n in sorted(type_counts.items(),
                                             key=lambda kv: -kv[1]))
    print(c("[GEONODE] ", "cyan") + f"Type breakdown -> {breakdown}")
    print(c("[GEONODE] ", "cyan") +
          f"Each proxy will be checked AS its own type for max accuracy. "
          f"All {c(str(len(unique)), 'bold')} will now be checked.\n")
    return unique


# ---------------------------------------------------------------------------
# Step 2: CHECK
# ---------------------------------------------------------------------------

def _safe_get(url: str, proxy: Proxy, proto: str, timeout: float) -> str | None:
    """GET `url` through the proxy USING a specific protocol (proto).

    Returns the (capped) response text on HTTP 200, else None.
      * allow_redirects=False -> proxy can't bounce us to a malicious URL.
      * verify=True           -> TLS certs enforced (HTTPS only).
      * stream + capped read  -> proxy can't flood us with a huge body.
      * response is never executed, only parsed for a short IP string.
    """
    # Split timeout: a short CONNECT budget so an unreachable proxy fails fast
    # (most dead proxies hang here), plus the full READ budget for slow-but-
    # alive proxies. (connect, read) tuple is honored by requests/urllib3.
    connect_timeout = min(4.0, timeout)
    resp = requests.get(
        url,
        proxies=proxy.proxies_dict(proto),   # connect AS this type
        headers=HEADERS,
        timeout=(connect_timeout, timeout),
        allow_redirects=False,
        verify=True,
        stream=True,
    )
    try:
        if resp.status_code != 200:
            return None
        raw = resp.raw.read(MAX_RESPONSE_BYTES, decode_content=True)
        if not raw:
            return ""
        return raw.decode("utf-8", "replace").strip()
    finally:
        resp.close()


def _parse_ip(text: str) -> str | None:
    try:
        import json as _json
        body = _json.loads(text)
        return body.get("origin") or body.get("ip") or body.get("query")
    except (ValueError, AttributeError):
        return text[:40] if text else None


def check_proxy(proxy: Proxy, timeout: float = 8.0, retries: int = 2) -> Proxy:
    """Type-aware, two-stage check with retries.

    Geonode tags each proxy with its real type(s) (socks4/socks5/http/https).
    We try each of THOSE types — a socks5 proxy is checked as socks5, an http
    proxy as http — instead of assuming one. This removes false 'dead' results
    that happen when a proxy is probed with the wrong protocol.

    Because free proxies are FLAKY (a good one randomly drops ~25% of the time),
    the liveness stage is attempted up to `retries`+1 times before a proxy is
    declared dead. This recovers proxies that hiccup on a single attempt and
    makes results far more stable/accurate run-to-run.

    1. LIVENESS (HTTP target): for each candidate type (and each retry), does
       the proxy forward traffic? First success -> alive + working_protocol.
    2. SECURITY (HTTPS target): can that same type carry verified TLS end-to-end?
       If yes the proxy only sees encrypted bytes -> proxy.secure = True.
    """
    # Candidate types = the proxy's own Geonode-declared protocols.
    candidates = proxy.protocols or ["socks4"]

    # --- Stage 1: liveness, trying each real type, with retries ---
    # Use ONE check-URL per attempt (rotated across attempts for endpoint
    # diversity) instead of all URLs every attempt. This avoids multiplying a
    # dead proxy's wait by len(CHECK_URLS): worst-case cost is now
    # (retries+1) x connect_timeout, not (retries+1) x #urls x timeout.
    for attempt in range(retries + 1):
        url = CHECK_URLS[attempt % len(CHECK_URLS)]
        for proto in candidates:
            start = time.perf_counter()
            try:
                text = _safe_get(url, proxy, proto, timeout)
                if text is not None:
                    proxy.alive = True
                    proxy.working_protocol = proto
                    proxy.latency_ms = round(
                        (time.perf_counter() - start) * 1000, 1)
                    proxy.exit_ip = _parse_ip(text)
                    break
            except Exception as exc:  # noqa: BLE001
                proxy.error = type(exc).__name__
                continue
        if proxy.alive:
            break
        # brief backoff before the next retry (skip after the last attempt)
        if attempt < retries:
            time.sleep(0.25)

    if not proxy.alive:
        return proxy

    # --- Stage 2: HTTPS capability over the type that worked ---
    working_proto = proxy.working_protocol or proxy.protocols[0]
    try:
        if _safe_get(HTTPS_CHECK_URL, proxy, working_proto,
                     max(4.0, timeout * 0.6)) is not None:
            proxy.secure = True
    except Exception:  # noqa: BLE001
        proxy.secure = False

    return proxy


def _format_save_line(p: Proxy) -> str:
    return f"{p.addr}  {p.protocol}  {'https' if p.secure else 'http'}\n"


def check_all(proxies: list[Proxy], workers: int = 50,
              timeout: float = 8.0, save_live: str | None = None,
              retries: int = 2) -> list[Proxy]:
    print(c("[CHECK] ", "cyan") +
          f"Checking all {c(str(len(proxies)), 'bold')} grabbed proxies "
          f"with {workers} parallel workers (timeout {timeout}s each, "
          f"{retries} retries before marking dead) ...")
    if save_live:
        # Start fresh; working proxies are appended LIVE as they're found.
        with open(save_live, "w", encoding="utf-8") as fh:
            fh.write("# ip:port  protocol  https_secure  (updated live)\n")
        print(c("[CHECK] ", "cyan") +
              f"Live-saving every working proxy to {c(save_live, 'bold')} "
              "as it is found ...\n")

    save_lock = threading.Lock()
    done = 0
    found = 0
    total = len(proxies)
    results: list[Proxy] = []
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(check_proxy, p, timeout, retries): p
                   for p in proxies}
        for fut in cf.as_completed(futures):
            p = fut.result()
            results.append(p)
            done += 1
            status = c("ALIVE", "green") if p.alive else c("dead ", "red")
            if p.alive:
                found += 1
                # Append to the file IMMEDIATELY (don't wait for the run to end).
                if save_live:
                    with save_lock:
                        with open(save_live, "a", encoding="utf-8") as fh:
                            fh.write(_format_save_line(p))
                            fh.flush()
                sec = c("HTTPS", "green") if p.secure else c("http ", "yellow")
                saved = c(" >saved", "cyan") if save_live else ""
                extra = (f"[{sec}] {p.latency_ms:>7.1f}ms  "
                         f"exit={p.exit_ip}{saved}")
            else:
                extra = c(p.error or "no response", "dim")
            # live progress line (shows running count of working found)
            print(f"  [{done:>4}/{total}] ({found} live) {status} "
                  f"{p.addr:<22} {extra}")
    print()
    return results


# ---------------------------------------------------------------------------
# Step 3: REPORT
# ---------------------------------------------------------------------------

def report(results: list[Proxy], save: str | None = None) -> None:
    working = sorted(
        [p for p in results if p.alive],
        key=lambda p: p.latency_ms or 9e9,
    )
    total = len(results)
    secure = [p for p in working if p.secure]

    print(c("=" * 70, "bold"))
    print(c(" RESULTS", "bold"))
    print(c("=" * 70, "bold"))
    print(f" Total grabbed       : {total}")
    print(f" Working             : {c(str(len(working)), 'green')}")
    print(f"   - HTTPS-secure    : {c(str(len(secure)), 'green')}  "
          + c("(safe for sensitive traffic)", "dim"))
    print(f"   - HTTP-only       : {c(str(len(working) - len(secure)), 'yellow')}  "
          + c("(usable, but can see/alter plain HTTP)", "dim"))
    print(f" Dead                : {c(str(total - len(working)), 'red')}")

    # Per-type accuracy view: how many of each protocol came back alive.
    by_type: dict[str, int] = {}
    for p in working:
        by_type[p.protocol] = by_type.get(p.protocol, 0) + 1
    if by_type:
        line = ", ".join(f"{c(str(n), 'green')} {t}"
                         for t, n in sorted(by_type.items(), key=lambda kv: -kv[1]))
        print(f" Working by type     : {line}")
    print(c("=" * 70, "bold"))

    if not working:
        print(c("\nNo working proxies found this run "
                "(free proxies are short-lived — try again).", "yellow"))
        return

    print(c("\n WORKING PROXIES (fastest first)\n", "green"))
    print(f" {'#':<4}{'IP:PORT':<24}{'TYPE':<9}{'HTTPS':<7}{'LATENCY':<11}{'EXIT IP'}")
    print(" " + "-" * 66)
    for i, p in enumerate(working, 1):
        https = c("yes", "green") if p.secure else c("no", "yellow")
        # pad on raw text length since color codes don't count visually
        https_cell = https + " " * (7 - (3 if p.secure else 2))
        print(f" {i:<4}{p.addr:<24}{p.protocol:<9}{https_cell}"
              f"{str(p.latency_ms) + 'ms':<11}{p.exit_ip or ''}")

    if save:
        with open(save, "w", encoding="utf-8") as fh:
            fh.write("# ip:port  protocol  https_secure\n")
            for p in working:
                fh.write(f"{p.addr}  {p.protocol}  "
                         f"{'https' if p.secure else 'http'}\n")
        print(c(f"\nFinalized {save}: {len(working)} working proxies "
                f"(re-sorted fastest-first, {len(secure)} HTTPS-secure).", "cyan"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Proxy grabber & checker (Geonode -> live check)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Max proxies to grab (default 0 = grab ALL available)")
    ap.add_argument("--workers", type=int, default=100,
                    help="Parallel check workers (default 100; balanced for "
                         "speed vs accuracy — see note, very high values can "
                         "cause false 'dead' results)")
    ap.add_argument("--timeout", type=float, default=8.0,
                    help="Per-proxy timeout seconds (default 8)")
    ap.add_argument("--retries", type=int, default=2,
                    help="Retries before marking a proxy dead (default 2). "
                         "Free proxies are flaky; retries recover ~25%% that "
                         "fail a single attempt -> more accurate results")
    ap.add_argument("--save", type=str, default="working_proxies.txt",
                    help="File to write working proxies (default working_proxies.txt)")
    # Defaults are wide-open ('any') so a plain run grabs the FULL list
    # (~10,000+ proxies / ~100+ pages). Add a flag to narrow it down.
    ap.add_argument("--protocol", type=str, default="any",
                    choices=["socks4", "socks5", "http", "https", "any"],
                    help="Protocol filter, or 'any' to grab all protocols (default any)")
    ap.add_argument("--country", type=str, default="any",
                    help="Country code (e.g. US, ID, IN) or 'any' for all countries (default any)")
    ap.add_argument("--anonymity", type=str, default="any",
                    choices=["elite", "anonymous", "transparent", "any"],
                    help="Anonymity level, or 'any' for all (default any)")
    ap.add_argument("--speed", type=str, default="any",
                    choices=["fast", "medium", "slow", "any"],
                    help="Speed filter, or 'any' for all (default any)")
    args = ap.parse_args()

    # Build filters from CLI; 'any' / '' drops that filter so the result set
    # widens (fewer filters = far more proxies = many more pages).
    filters = {"sort_by": "lastChecked", "sort_type": "desc"}
    if args.country.lower() not in ("any", ""):
        filters["country"] = args.country.upper()
    if args.anonymity.lower() != "any":
        filters["anonymityLevel"] = args.anonymity
    if args.protocol.lower() != "any":
        filters["protocols"] = args.protocol
    if args.speed.lower() != "any":
        filters["speed"] = args.speed

    proxies = grab_proxies(limit=args.limit, filters=filters)
    if not proxies:
        print(c("No proxies grabbed — aborting.", "red"))
        sys.exit(1)

    results = check_all(proxies, workers=args.workers, timeout=args.timeout,
                        save_live=args.save, retries=args.retries)
    report(results, save=args.save)


if __name__ == "__main__":
    main()
