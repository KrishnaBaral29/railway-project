#!/usr/bin/env python3
"""
Proxy Grabber — Web app
=======================

A thin Flask layer over the existing CLI engine (proxy_grabber.py). It streams
live results to the browser as NDJSON (one JSON object per line) so the UI can
show each proxy the moment it's checked — same behaviour as the terminal tool.

Endpoints:
    GET  /              -> the single-page UI
    POST /api/run       -> NDJSON stream; mode = "grab" (Geonode) or "recheck"
    GET  /healthz       -> health probe

Run locally:   python app.py
On Railway:    gunicorn app:app  (see Procfile)
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import os

from flask import Flask, Response, render_template, request

from proxy_grabber import (
    Proxy,
    check_proxy,
    grab_proxies,
    _fetch_page,
)

app = Flask(__name__)

# Safety caps so a single request can't exhaust the server.
MAX_LIMIT = 1500
MAX_WORKERS = 400


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clamp(v, lo, hi, default):
    try:
        v = type(default)(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _emit(event_type: str, **payload) -> str:
    """Serialize one NDJSON event line."""
    payload["type"] = event_type
    return json.dumps(payload) + "\n"


def _build_filters(data: dict) -> dict:
    """Build a Geonode filter dict from request data ('any' drops a filter)."""
    filters = {"sort_by": "lastChecked", "sort_type": "desc"}
    country = str(data.get("country", "any")).strip()
    anonymity = str(data.get("anonymity", "any")).strip().lower()
    protocol = str(data.get("protocol", "any")).strip().lower()
    speed = str(data.get("speed", "any")).strip().lower()
    if country.lower() not in ("any", ""):
        filters["country"] = country.upper()
    if anonymity not in ("any", ""):
        filters["anonymityLevel"] = anonymity
    if protocol not in ("any", ""):
        filters["protocols"] = protocol
    if speed not in ("any", ""):
        filters["speed"] = speed
    return filters


def _parse_pasted(text: str) -> list[Proxy]:
    """Turn pasted 'ip:port [protocol]' lines into Proxy objects (deduped)."""
    seen: set[str] = set()
    proxies: list[Proxy] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        addr = parts[0]
        proto = (parts[1].lower() if len(parts) > 1 and parts[1].lower()
                 in ("socks4", "socks5", "http", "https") else None)
        if "://" in addr:
            scheme, addr = addr.split("://", 1)
            proto = proto or scheme.replace("h", "").replace("a", "")
        if ":" not in addr or addr in seen:
            continue
        ip, _, port = addr.rpartition(":")
        if not ip or not port.isdigit():
            continue
        seen.add(addr)
        protocols = [proto] if proto else ["socks4", "socks5", "http"]
        proxies.append(Proxy(ip=ip, port=int(port), protocols=protocols))
    return proxies


def _breakdown(proxies: list[Proxy]) -> dict:
    counts: dict[str, int] = {}
    for p in proxies:
        for proto in p.protocols:
            counts[proto] = counts.get(proto, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Streaming engine
# ---------------------------------------------------------------------------

def _stream_check(proxies, workers, timeout, retries):
    """Yield NDJSON events for each checked proxy + a final summary."""
    total = len(proxies)
    done = found = secure = 0
    yield _emit("check_start", total=total, workers=workers,
                timeout=timeout, retries=retries)

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(check_proxy, p, timeout, retries): p
                   for p in proxies}
        for fut in cf.as_completed(futures):
            p = fut.result()
            done += 1
            if p.alive:
                found += 1
                if p.secure:
                    secure += 1
            yield _emit(
                "proxy",
                done=done, total=total, found=found, secure=secure,
                addr=p.addr, alive=p.alive,
                protocol=p.protocol if p.alive else (p.protocols[0] if p.protocols else "?"),
                https=p.secure,
                latency=p.latency_ms,
                exit_ip=p.exit_ip,
                error=p.error,
            )

    yield _emit("summary", total=total, working=found, secure=secure,
                dead=total - found)


def _event_stream(data: dict):
    mode = data.get("mode", "grab")
    workers = _clamp(data.get("workers", 100), 1, MAX_WORKERS, 100)
    timeout = _clamp(data.get("timeout", 8), 1.0, 30.0, 8.0)
    retries = _clamp(data.get("retries", 2), 0, 5, 2)

    try:
        if mode == "recheck":
            proxies = _parse_pasted(data.get("proxies_text", ""))
            if not proxies:
                yield _emit("error", message="No valid proxies found in the input.")
                return
            yield _emit("grabbed", count=len(proxies),
                        breakdown=_breakdown(proxies), source="pasted list")
        else:
            limit = _clamp(data.get("limit", 300), 1, MAX_LIMIT, 300)
            filters = _build_filters(data)
            shown = ", ".join(f"{k}={v}" for k, v in filters.items()
                              if k not in ("sort_by", "sort_type")) or "all proxies"
            yield _emit("status", message=f"Connecting to Geonode ({shown}) ...")

            # Quick total for the "Detected N" stat.
            try:
                total_avail = _fetch_page(filters, 1, 1).get("total", 0)
                yield _emit("detected", total=total_avail)
            except Exception:  # noqa: BLE001
                pass

            yield _emit("status", message="Grabbing & de-duplicating proxies ...")
            proxies = grab_proxies(limit=limit, filters=filters)
            if not proxies:
                yield _emit("error", message="Geonode returned no proxies for those filters.")
                return
            yield _emit("grabbed", count=len(proxies),
                        breakdown=_breakdown(proxies), source="Geonode")

        yield from _stream_check(proxies, workers, timeout, retries)

    except Exception as exc:  # noqa: BLE001
        yield _emit("error", message=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/run", methods=["POST"])
def run():
    data = request.get_json(silent=True) or {}

    def generate():
        for line in _event_stream(data):
            yield line

    return Response(
        generate(),
        mimetype="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable proxy buffering (nginx/railway)
        },
    )


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
