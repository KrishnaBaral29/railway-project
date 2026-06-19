# Proxy Grabber & Checker

A fast, accurate proxy grabber and live-checker. It scrapes the full free-proxy
list from [Geonode](https://geonode.com/free-proxy-list), checks every proxy **as
its real type** (SOCKS4 / SOCKS5 / HTTP / HTTPS), and reports the working ones —
with a security flag for which can safely carry HTTPS/TLS traffic.

## Features

- **Fast grabbing** — pulls all ~10,000 listed proxies in a few seconds (500/page
  via the Geonode API + concurrent page fetching).
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

## Usage

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
