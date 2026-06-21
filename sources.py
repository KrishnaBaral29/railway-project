#!/usr/bin/env python3
"""
Multi-source proxy grabber
==========================

Pulls proxies from several free lists at once, de-duplicates, and interleaves
them round-robin so a given grab_limit is filled with a fair mix from every
source (instead of all from one). More sources + fresher/validated lists ->
much higher live rate than Geonode's free list alone.

Sources:
  - geonode     : JSON API (supports country / anonymity / speed / protocol)
  - proxifly    : github raw, protocol://ip:port, refreshed often
  - monosans    : github raw, validated list, protocol://ip:port
  - proxyscrape : ProxyScrape v4 API, protocol://ip:port
  - thespeedx   : github raw, ip:port per-protocol files (large pool)

Raw-list sources have no country/anonymity/speed metadata, so those filters
apply to Geonode only; protocol filtering applies to every source.
"""

from __future__ import annotations

import concurrent.futures as cf
from itertools import zip_longest

import requests

from proxy_grabber import HEADERS, Proxy, grab_proxies

VALID_PROTOS = ("socks4", "socks5", "http", "https")

# Raw text/list endpoints.
URL_PROXIFLY = "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"
URL_MONOSANS = "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/all.txt"
URL_PROXYSCRAPE = ("https://api.proxyscrape.com/v4/free-proxy-list/get"
                   "?request=display_proxies&proxy_format=protocolipport&format=text")
URL_THESPEEDX = {
    "socks5": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "socks4": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt",
    "http":   "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
}

# Human-friendly source labels (order also sets round-robin priority — fresher
# / validated lists first so they're favored when truncating to the limit).
SOURCE_ORDER = ["monosans", "proxifly", "proxyscrape", "geonode", "thespeedx"]


def _norm_proto(scheme: str) -> str | None:
    s = scheme.strip().lower()
    if s.startswith("socks5"):
        return "socks5"
    if s.startswith("socks4"):
        return "socks4"
    if s == "https":
        return "https"
    if s == "http":
        return "http"
    return None


def _parse_scheme_lines(text: str) -> list[tuple[str, int, list[str]]]:
    """Parse 'protocol://ip:port' lines."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if "://" not in line:
            continue
        scheme, _, addr = line.partition("://")
        proto = _norm_proto(scheme)
        if not proto or ":" not in addr:
            continue
        ip, _, port = addr.rpartition(":")
        if ip and port.isdigit():
            out.append((ip, int(port), [proto]))
    return out


def _parse_ipport_lines(text: str, proto: str) -> list[tuple[str, int, list[str]]]:
    """Parse bare 'ip:port' lines (protocol implied by the file)."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" in line:
            line = line.split("://", 1)[1]
        ip, _, port = line.rpartition(":")
        if ip and port.isdigit():
            out.append((ip, int(port), [proto]))
    return out


def _get(url: str, timeout: float = 20) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text


# ---- individual source fetchers (each returns [(ip, port, [protocols])]) ----

def _src_proxifly():
    return _parse_scheme_lines(_get(URL_PROXIFLY))


def _src_monosans():
    return _parse_scheme_lines(_get(URL_MONOSANS))


def _src_proxyscrape():
    return _parse_scheme_lines(_get(URL_PROXYSCRAPE))


def _src_thespeedx():
    out = []
    for proto, url in URL_THESPEEDX.items():
        try:
            out += _parse_ipport_lines(_get(url), proto)
        except Exception:  # noqa: BLE001
            continue
    return out


def _src_geonode(limit, filters):
    # grab_proxies returns Proxy objects; normalize to tuples.
    proxies = grab_proxies(limit=limit, filters=filters)
    return [(p.ip, p.port, list(p.protocols)) for p in proxies]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def grab_multi(limit=300, protocol="any", country="any",
               anonymity="any", speed="any"):
    """Grab up to `limit` unique proxies from all sources, interleaved.

    Returns (proxies: list[Proxy], stats: dict).
    stats = {'fetched': {source: n}, 'used': {source: n}, 'total_unique': n}.
    """
    protocol = (protocol or "any").lower()
    country = (country or "any")

    # Geonode filters (raw sources ignore country/anonymity/speed).
    geo_filters = {"sort_by": "lastChecked", "sort_type": "desc"}
    if country.lower() not in ("any", ""):
        geo_filters["country"] = country.upper()
    if anonymity.lower() not in ("any", ""):
        geo_filters["anonymityLevel"] = anonymity.lower()
    if speed.lower() not in ("any", ""):
        geo_filters["speed"] = speed.lower()
    if protocol != "any":
        geo_filters["protocols"] = protocol

    # When a specific country is requested, only Geonode knows geolocation,
    # so use it alone to honor the filter. Otherwise pull from everything.
    if country.lower() not in ("any", ""):
        active = ["geonode"]
    else:
        active = list(SOURCE_ORDER)

    fetchers = {
        "geonode": lambda: _src_geonode(max(limit, 200), geo_filters),
        "proxifly": _src_proxifly,
        "monosans": _src_monosans,
        "proxyscrape": _src_proxyscrape,
        "thespeedx": _src_thespeedx,
    }

    # Fetch all active sources concurrently.
    raw: dict[str, list] = {}
    with cf.ThreadPoolExecutor(max_workers=len(active)) as pool:
        futs = {pool.submit(fetchers[name]): name for name in active}
        for fut in cf.as_completed(futs):
            name = futs[fut]
            try:
                raw[name] = fut.result() or []
            except Exception:  # noqa: BLE001
                raw[name] = []

    # Apply protocol filter to every source's list.
    def keep(protos):
        if protocol == "any":
            return True
        return protocol in protos

    pools = []
    fetched = {}
    for name in active:
        items = [t for t in raw.get(name, []) if keep(t[2])]
        fetched[name] = len(items)
        pools.append((name, items))

    # Interleave round-robin (one from each source per round) for diversity,
    # de-duplicate by ip:port, merge protocols, truncate to limit.
    seen: dict[str, Proxy] = {}
    ordered: list[Proxy] = []
    used = {name: 0 for name, _ in pools}
    iters = [(name, iter(items)) for name, items in pools]
    rows = zip_longest(*[items for _, items in pools])
    for rnd in rows:
        for (name, _), cell in zip(iters, rnd):
            if cell is None:
                continue
            ip, port, protos = cell
            addr = f"{ip}:{port}"
            if addr in seen:
                ex = seen[addr]
                for pr in protos:
                    if pr not in ex.protocols:
                        ex.protocols.append(pr)
                continue
            obj = Proxy(ip=ip, port=int(port),
                        protocols=list(dict.fromkeys(protos)) or ["socks4"])
            seen[addr] = obj
            ordered.append(obj)
            used[name] += 1
            if len(ordered) >= limit:
                break
        if len(ordered) >= limit:
            break

    stats = {"fetched": fetched, "used": used, "total_unique": len(seen),
             "sources": active}
    return ordered, stats
