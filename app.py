#!/usr/bin/env python3
"""
Proxy Grabber — Web app (polling model)
=======================================

A thin Flask layer over the existing CLI engine (proxy_grabber.py).

Why polling instead of streaming: when this app is reached through a reverse
proxy that buffers the whole response body until end-of-response (code-server's
/proxy/<port>/, Hugging Face Spaces' edge router, some CDNs), a single long
streamed response shows NOTHING until the run finishes, then dumps everything at
once. No header/padding/flush trick reliably beats full-response buffering.

Short-polling is immune by construction: every /status reply is a small,
*complete* response, so the proxy has no long-lived body to hold. The browser
gets live progress through any proxy.

Endpoints:
    GET  /                       -> the single-page UI
    POST /api/start              -> {job_id}; spawns a background worker
    GET  /api/status/<id>?cursor=N -> new events since N + phase
    POST /api/stop               -> {job_id} signals the worker to halt
    GET  /healthz                -> health probe

Run locally:   python app.py
On a host:     gunicorn app:app --workers 1 --threads 16  (single process so the
               in-memory job store is shared across request threads)
"""

from __future__ import annotations

import concurrent.futures as cf
import os
import threading
import time
import uuid

from flask import Flask, jsonify, render_template, request

from proxy_grabber import Proxy, check_proxy
from sources import grab_multi

app = Flask(__name__)

# Safety caps so a single request can't exhaust the server.
MAX_LIMIT = 1500
MAX_WORKERS = 400

# In-memory job store (single process). job_id -> Job.
JOBS: dict[str, "Job"] = {}
JOBS_LOCK = threading.Lock()
JOB_TTL = 600        # seconds to keep a finished job around for late polls
JOB_MAX_AGE = 1800   # hard cap; reap any job older than this


class Job:
    def __init__(self):
        self.id = uuid.uuid4().hex
        self.results: list[dict] = []   # ordered event log (append-only)
        self.phase = "running"          # running | done | stopped | error
        self.error: str | None = None
        self.created_at = time.time()
        self.finished_at: float | None = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()    # guards results/phase reads+writes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clamp(v, lo, hi, default):
    try:
        v = type(default)(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


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


def _reap_jobs() -> None:
    """Drop finished/old jobs so the store doesn't grow unbounded."""
    now = time.time()
    with JOBS_LOCK:
        for jid in list(JOBS):
            j = JOBS[jid]
            if (j.finished_at and now - j.finished_at > JOB_TTL) or \
               (now - j.created_at > JOB_MAX_AGE):
                del JOBS[jid]


# ---------------------------------------------------------------------------
# Worker — runs in a background thread, appends events to the job log
# ---------------------------------------------------------------------------

def _run_job(job: Job, data: dict) -> None:
    def emit(**ev):
        with job.lock:
            job.results.append(ev)

    try:
        mode = data.get("mode", "grab")
        workers = _clamp(data.get("workers", 100), 1, MAX_WORKERS, 100)
        timeout = _clamp(data.get("timeout", 7), 1.0, 30.0, 7.0)
        retries = _clamp(data.get("retries", 1), 0, 5, 1)

        # ---- acquire the proxy list ----
        if mode == "recheck":
            proxies = _parse_pasted(data.get("proxies_text", ""))
            if not proxies:
                emit(type="error", message="No valid proxies found in the input.")
                job.error = "No valid proxies found in the input."
                job.phase = "error"
                return
            emit(type="grabbed", count=len(proxies),
                 breakdown=_breakdown(proxies), source="pasted list")
        else:
            limit = _clamp(data.get("limit", 300), 1, MAX_LIMIT, 300)
            emit(type="status",
                 message="Grabbing proxies from all sources (Geonode, "
                         "proxifly, monosans, proxyscrape, TheSpeedX) …")
            proxies, stats = grab_multi(
                limit=limit,
                protocol=str(data.get("protocol", "any")),
                country=str(data.get("country", "any")),
                anonymity=str(data.get("anonymity", "any")),
                speed=str(data.get("speed", "any")),
            )
            if not proxies:
                emit(type="error", message="No proxies returned from any source.")
                job.error = "no proxies"
                job.phase = "error"
                return
            used = ", ".join(f"{n}:{c}" for n, c in stats["used"].items() if c)
            emit(type="grabbed", count=len(proxies),
                 breakdown=_breakdown(proxies),
                 source=f"{len(stats['sources'])} sources ({used})")

        # ---- check every proxy, emitting one event each ----
        total = len(proxies)
        done = found = secure = 0
        emit(type="check_start", total=total, workers=workers,
             timeout=timeout, retries=retries)

        pool = cf.ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {pool.submit(check_proxy, p, timeout, retries): p
                       for p in proxies}
            pending = set(futures)
            # Wait in short slices so a Stop is honored within ~0.5s even when
            # no check has completed yet (dead proxies can take seconds).
            while pending:
                if job.stop_event.is_set():
                    job.phase = "stopped"
                    break
                finished, pending = cf.wait(
                    pending, timeout=0.5, return_when=cf.FIRST_COMPLETED)
                for fut in finished:
                    p = fut.result()
                    done += 1
                    if p.alive:
                        found += 1
                        if p.secure:
                            secure += 1
                    emit(
                        type="proxy",
                        done=done, total=total, found=found, secure=secure,
                        addr=p.addr, alive=p.alive,
                        protocol=p.protocol if p.alive else (p.protocols[0] if p.protocols else "?"),
                        https=p.secure, latency=p.latency_ms,
                        exit_ip=p.exit_ip, error=p.error,
                        country=p.country or "Unknown",
                    )
        finally:
            # Don't block on in-flight checks if we were stopped.
            pool.shutdown(wait=False, cancel_futures=True)

        if job.phase != "stopped":
            emit(type="summary", total=total, working=found,
                 secure=secure, dead=total - found)
            job.phase = "done"

    except Exception as exc:  # noqa: BLE001
        emit(type="error", message=f"{type(exc).__name__}: {exc}")
        job.error = str(exc)
        job.phase = "error"
    finally:
        job.finished_at = time.time()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start", methods=["POST"])
def start():
    data = request.get_json(silent=True) or {}
    _reap_jobs()
    job = Job()
    with JOBS_LOCK:
        JOBS[job.id] = job
    threading.Thread(target=_run_job, args=(job, data), daemon=True).start()
    return jsonify({"job_id": job.id})


@app.route("/api/status/<job_id>")
def status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        return jsonify({
            "error": "unknown or expired job",
            "phase": "error", "results": [], "cursor": 0, "done": True,
        }), 404
    try:
        cursor = max(0, int(request.args.get("cursor", 0)))
    except (TypeError, ValueError):
        cursor = 0
    with job.lock:
        new = job.results[cursor:]
        phase = job.phase
        err = job.error
    return jsonify({
        "results": new,
        "cursor": cursor + len(new),
        "phase": phase,
        "error": err,
        "done": phase in ("done", "stopped", "error"),
    })


@app.route("/api/stop", methods=["POST"])
def stop():
    data = request.get_json(silent=True) or {}
    with JOBS_LOCK:
        job = JOBS.get(data.get("job_id"))
    if job is None:
        return jsonify({"ok": False}), 404
    job.stop_event.set()
    return jsonify({"ok": True})


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
