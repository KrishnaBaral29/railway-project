# ProxyForge — Proxy Grabber & Checker

A fast, accurate proxy grabber and live-checker, available as both a **CLI** and a
**web app**. It scrapes the full free-proxy list from
[Geonode](https://geonode.com/free-proxy-list), checks every proxy **as its real
type** (SOCKS4 / SOCKS5 / HTTP / HTTPS), and reports the working ones — with a
security flag for which can safely carry HTTPS/TLS traffic.

> **Web UI:** grab → test → verify, all live in the browser with animated stats
> and a results table that streams in as each proxy is confirmed. One-click
> deploy to Railway.

## Features

- **Multi-source grabbing** — pulls from 5 live sources at once (Geonode,
  proxifly, monosans, proxyscrape, TheSpeedX), de-duplicates, and interleaves
  them round-robin so your grab limit is a fair mix from every source. Using
  fresher/validated lists lifts the live rate from ~3% (Geonode alone) to
  ~35-45%.
- **Fast grabbing** — sources are fetched concurrently; a batch lands in ~1-2s.
- **De-duplication** — repeated `ip:port` entries are never counted.
- **Type-aware checking** — each proxy is tested using its actual protocol, so a
  SOCKS5 proxy isn't falsely marked dead by probing it as SOCKS4. (~40% hit rate
  vs ~3% when assuming one type.)
- **Retries for accuracy** — free proxies are flaky; each gets multiple attempts
  before being declared dead (recovers ~34% that fail a single attempt and makes
  results far more consistent run-to-run).
- **Security hardening** — checks route through HTTPS with TLS verification,
  redirects disabled, and response size capped, so a hostile proxy can't read,
  tamper, redirect, or flood. Each working proxy is flagged `https` (TLS-safe) or
  `http` (usable, but can see/alter plain HTTP).
- **Live saving** — working proxies are written to `working_proxies.txt` the
  moment they're found, so you keep results even if you stop early.
- **Re-checker** — `recheck.py` re-tests a saved list using the identical method.

## Install

```bash
pip install -r requirements.txt
```

## Web app

```bash
python app.py            # then open http://localhost:5000
```

The page has two tabs:

- **⚡ Grab & Test** — pick filters (protocol, country, anonymity, speed) and a
  limit, then watch proxies stream in live as they're grabbed and verified.
- **🔁 Re-check List** — paste a saved list (`ip:port` per line) and re-test it.

Results update in real time (NDJSON streaming) with animated counters for
*Listed / Grabbed / Working / HTTPS-secure / Dead*, and you can copy or download
the working set.

## Deploy to Railway

This repo is Railway-ready (`Procfile`, `railway.json`, `requirements.txt`,
`.python-version`).

1. Push to GitHub (already done).
2. On [railway.app](https://railway.app): **New Project → Deploy from GitHub repo**
   → pick this repo.
3. Railway auto-detects Python (Nixpacks), installs `requirements.txt`, and runs
   the `gunicorn` start command. No env vars required — it binds to `$PORT`
   automatically.
4. Open the generated public URL. Done. 🚀

## CLI usage

### Grab + check

```bash
# Grab ALL proxies (all types, all countries) and check them
python proxy_grabber.py

# Narrow the net with filters
python proxy_grabber.py --country US --protocol socks4 --anonymity elite --speed fast

# Tuning
python proxy_grabber.py --workers 200 --timeout 8 --retries 2 --limit 500
```

Key flags:

| Flag | Default | Meaning |
|------|---------|---------|
| `--limit` | `0` (all) | Max proxies to grab |
| `--workers` | `100` | Parallel check workers |
| `--timeout` | `8` | Per-proxy timeout (seconds) |
| `--retries` | `2` | Attempts before marking a proxy dead |
| `--protocol` | `any` | `socks4` / `socks5` / `http` / `https` / `any` |
| `--country` | `any` | Country code (e.g. `US`) or `any` |
| `--anonymity` | `any` | `elite` / `anonymous` / `transparent` / `any` |
| `--speed` | `any` | `fast` / `medium` / `slow` / `any` |
| `--save` | `working_proxies.txt` | Output file |

### Re-check a saved list

```bash
python recheck.py                 # re-test working_proxies.txt
python recheck.py mylist.txt      # re-test any file
python recheck.py --save          # rewrite file, keeping only survivors
```

## Output format

`working_proxies.txt` is written as:

```
# ip:port  protocol  https_secure
184.178.172.14:4145  socks4  http
206.189.181.32:9050  socks4  https
```

## Notes

Free public proxies are short-lived (often dead within minutes) and inherently
unstable. Use the `https`-flagged proxies for anything sensitive; the `http`-only
ones are fine for casual use over HTTPS sites (TLS still protects you end-to-end).
