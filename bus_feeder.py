#!/usr/bin/env python3
"""Push next-bus arrival times for two SG bus services to two Ulanzi clocks.

Data source: LTA DataMall's Bus Arrival API v3 (the source LTA operators'
apps, including SMRT's, ultimately pull from), called directly with an
AccountKey from .env. Falls back automatically to arrivelah2.busrouter.sg
(a free, no-key proxy in front of the same API) if the direct call fails
for any reason, including a malformed response.

Target clocks come from CLOCKS in .env as comma-separated ip=kind pairs, e.g.
CLOCKS=192.168.1.30=awtrix,192.168.1.31=awtrix_tc002. Each kind needs its own
push format:
  - awtrix       -> AWTRIX NG on a 32x8 clock (TC001): PUT /api/v1/apps/pushed/<name>,
                    DELETE /api/v1/apps/<name> to clear
  - awtrix_tc002 -> AWTRIX NG on a 52x16 Ulanzi TC002: same API, sent as a
                    full-panel `layout` (icon box + one text box per service)
  - ulanzi_tc002 -> TC002 on stock Ulanzi firmware: POST /api/custom?name=<name>,
                    {} body to clear
See README.md.
"""
import base64
import os
import re
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import time as dtime
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

KINDS = ("awtrix", "awtrix_tc002", "ulanzi_tc002")

LTA_BUS_ARRIVAL_URL = "https://datamall2.mytransport.sg/ltaodataservice/v3/BusArrival"
ARRIVELAH_URL = "https://arrivelah2.busrouter.sg/"
API_TIMEOUT = 8  # seconds, LTA / arrivelah
CLOCK_TIMEOUT = 3  # seconds, clocks are on the LAN

ICONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
# Generated from icon.jpeg (the Singapore Public Transport symbol) via
# generate_transport_icon.py. Neither the source image nor these two
# generated files are committed to git (trademarked/non-free mark, see
# README.md). Regenerate locally from your own copy of icon.jpeg if missing.
AWTRIX_ICON_NAME = "sgpt_wave"  # uploaded once, referenced by ID thereafter
# ^ AWTRIX NG appears to cache a loaded icon's bitmap in memory keyed by
# this name, independent of the file's on-disk content. Replacing the
# file under the same name did NOT refresh what was shown on the panel.
# Bumping the id forces it to load fresh. If you ever regenerate this
# icon again, bump the name again (or power-cycle the clock) rather than
# just re-uploading under the same name.
AWTRIX_ICON_PATH = os.path.join(ICONS_DIR, "transport_icon_8.gif")
TC002_ICON_PATH = os.path.join(ICONS_DIR, "transport_icon_16.gif")

_BUS_STOP_RE = re.compile(r"^\d{5}$")
_SERVICE_RE = re.compile(r"^[A-Za-z0-9]{1,6}$")
_APP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(:\d{1,5})?$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# Populated by configure() from the environment (.env) in main().
CONFIG = {}
CLOCKS = {}


class FetchError(Exception):
    """An arrivals source answered, but with something we can't trust."""


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def warn(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} [warn] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def parse_clocks(spec):
    """'ip=kind,ip=kind' -> {ip: kind}"""
    clocks = {}
    for part in filter(None, (p.strip() for p in spec.split(","))):
        ip, _, kind = part.partition("=")
        ip, kind = ip.strip(), kind.strip()
        if kind not in KINDS:
            raise SystemExit(f"CLOCKS: unknown kind {kind!r} for {ip!r} (expected one of {', '.join(KINDS)})")
        if not _HOST_RE.match(ip):
            raise SystemExit(f"CLOCKS: {ip!r} is not a host/IP (optionally :port)")
        clocks[ip] = kind
    return clocks


def parse_quiet_hours(spec):
    """'01:00-05:30' -> (time(1, 0), time(5, 30)); '' -> None."""
    spec = spec.strip()
    if not spec:
        return None
    start, sep, end = spec.partition("-")
    m1, m2 = _HHMM_RE.match(start.strip()), _HHMM_RE.match(end.strip())
    if not (sep and m1 and m2):
        raise SystemExit(f"QUIET_HOURS: expected HH:MM-HH:MM, got {spec!r}")
    return dtime(int(m1[1]), int(m1[2])), dtime(int(m2[1]), int(m2[2]))


def in_quiet_hours(window, now):
    if not window:
        return False
    start, end = window
    t = now.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # window crosses midnight


def _int_env(env, name, default):
    raw = env.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name}: expected an integer, got {raw!r}") from None
    if value <= 0:
        raise SystemExit(f"{name}: must be > 0, got {value}")
    return value


def load_config(env):
    """Build and validate (CONFIG, CLOCKS) from an environment mapping."""
    cfg = {
        "BUS_STOP_CODE": env.get("BUS_STOP_CODE", "64409").strip(),  # Blk 477A Upp Serangoon Rd
        "SERVICES": [s.strip() for s in env.get("SERVICES", "102,136").split(",") if s.strip()],
        "APP_NAME": env.get("APP_NAME", "sgbus").strip(),
        "POLL_SECONDS": _int_env(env, "POLL_SECONDS", 30),
        "AWTRIX_DURATION_MS": _int_env(env, "AWTRIX_DURATION_MS", 8000),
        "TC002_DURATION": _int_env(env, "TC002_DURATION", 10),  # seconds
        "QUIET_HOURS": parse_quiet_hours(env.get("QUIET_HOURS", "")),
        "LTA_ACCOUNT_KEY": (env.get("AccountKey") or "").strip() or None,
    }
    if not _BUS_STOP_RE.match(cfg["BUS_STOP_CODE"]):
        raise SystemExit(f"BUS_STOP_CODE: expected 5 digits, got {cfg['BUS_STOP_CODE']!r}")
    if not cfg["SERVICES"]:
        raise SystemExit("SERVICES: set at least one bus service number")
    bad = [s for s in cfg["SERVICES"] if not _SERVICE_RE.match(s)]
    if bad:
        raise SystemExit(f"SERVICES: invalid service number(s) {bad}")
    if not _APP_NAME_RE.match(cfg["APP_NAME"]):
        raise SystemExit(f"APP_NAME: use letters, digits, _ or - only, got {cfg['APP_NAME']!r}")

    clocks = parse_clocks(env.get("CLOCKS", ""))
    if not clocks:
        raise SystemExit("Set CLOCKS in .env, e.g. CLOCKS=192.168.1.30=awtrix,192.168.1.31=awtrix_tc002")
    return cfg, clocks


def configure(env=None):
    global CONFIG, CLOCKS
    if env is None:
        load_dotenv()
        env = os.environ
    CONFIG, CLOCKS = load_config(env)


def _app_path():
    return quote(CONFIG["APP_NAME"], safe="")


# --------------------------------------------------------------------------
# icons
# --------------------------------------------------------------------------

_tc002_icon_cache = {}


def tc002_icon_b64():
    """Base64 of the 16x16 TC002 icon, or None if the file isn't there.

    Loaded lazily (only TC002 clocks need it) and cached. A missing file
    just means pushing without an icon, not a crash at startup.
    """
    if "b64" not in _tc002_icon_cache:
        try:
            with open(TC002_ICON_PATH, "rb") as f:
                _tc002_icon_cache["b64"] = base64.b64encode(f.read()).decode()
        except OSError as e:
            warn(f"TC002 icon unavailable, pushing without it: {e}")
            _tc002_icon_cache["b64"] = None
    return _tc002_icon_cache["b64"]


def ensure_awtrix_icon():
    """Upload the transport-symbol icon to every AWTRIX TC001 clock missing it."""
    for ip in (ip for ip, kind in CLOCKS.items() if kind == "awtrix"):
        try:
            r = requests.get(f"http://{ip}/api/v1/files", params={"dir": "/ICONS"}, timeout=CLOCK_TIMEOUT)
            r.raise_for_status()
            names = {f.get("name") for f in r.json().get("files", []) if isinstance(f, dict)}
            if f"{AWTRIX_ICON_NAME}.gif" in names:
                continue
            with open(AWTRIX_ICON_PATH, "rb") as f:
                up = requests.post(
                    f"http://{ip}/api/v1/files",
                    params={"dir": "/ICONS"},
                    files={"file": (f"{AWTRIX_ICON_NAME}.gif", f, "image/gif")},
                    timeout=10,
                )
            up.raise_for_status()
            log(f"uploaded {AWTRIX_ICON_NAME}.gif to AWTRIX clock ({ip})")
        except (requests.RequestException, OSError, ValueError, AttributeError) as e:
            warn(f"could not ensure AWTRIX icon on {ip}: {e}")


# --------------------------------------------------------------------------
# fetching arrivals
# --------------------------------------------------------------------------

def _make_api_session():
    # One pooled session for the HTTPS APIs. A single quick retry with
    # backoff on connection errors / 429 / 5xx, on top of the
    # LTA -> arrivelah fallback.
    s = requests.Session()
    retry = Retry(
        total=1,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


API = _make_api_session()


def minutes_until_ms(duration_ms):
    if duration_ms is None:
        return None
    try:
        return max(0, round(float(duration_ms) / 60000))
    except (TypeError, ValueError):
        return None


def minutes_until_iso(iso_str, now=None):
    if not iso_str or not isinstance(iso_str, str):
        return None
    try:
        arrival = datetime.fromisoformat(iso_str)
    except ValueError:
        return None
    now = now or datetime.now(arrival.tzinfo)
    try:
        return max(0, round((arrival - now).total_seconds() / 60))
    except TypeError:  # naive vs aware mix
        return None


def parse_lta(data):
    """LTA BusArrival v3 JSON -> {service_no: minutes or None}."""
    services = data.get("Services") if isinstance(data, dict) else None
    if not isinstance(services, list):
        raise FetchError("LTA response has no Services list")
    minutes_by_no = {}
    for svc in services:
        if not isinstance(svc, dict) or not svc.get("ServiceNo"):
            continue
        next_bus = svc.get("NextBus")
        iso = next_bus.get("EstimatedArrival") if isinstance(next_bus, dict) else None
        minutes_by_no[str(svc["ServiceNo"])] = minutes_until_iso(iso)
    return minutes_by_no


def parse_arrivelah(data):
    """arrivelah2 JSON -> {service_no: minutes or None}."""
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, list):
        raise FetchError("arrivelah response has no services list")
    minutes_by_no = {}
    for svc in services:
        if not isinstance(svc, dict) or not svc.get("no"):
            continue
        nxt = svc.get("next")
        ms = nxt.get("duration_ms") if isinstance(nxt, dict) else None
        minutes_by_no[str(svc["no"])] = minutes_until_ms(ms)
    return minutes_by_no


def fetch_lta_minutes(bus_stop_code, account_key):
    r = API.get(
        LTA_BUS_ARRIVAL_URL,
        params={"BusStopCode": bus_stop_code},
        headers={"AccountKey": account_key, "accept": "application/json"},
        timeout=API_TIMEOUT,
    )
    r.raise_for_status()
    return parse_lta(r.json())


def fetch_arrivelah_minutes(bus_stop_code):
    r = API.get(ARRIVELAH_URL, params={"id": bus_stop_code}, timeout=API_TIMEOUT)
    r.raise_for_status()
    return parse_arrivelah(r.json())


def fetch_minutes(bus_stop_code):
    if CONFIG["LTA_ACCOUNT_KEY"]:
        try:
            return fetch_lta_minutes(bus_stop_code, CONFIG["LTA_ACCOUNT_KEY"]), "lta"
        except (requests.RequestException, FetchError) as e:
            warn(f"LTA DataMall fetch failed ({e}), falling back to arrivelah")
    return fetch_arrivelah_minutes(bus_stop_code), "arrivelah"


# --------------------------------------------------------------------------
# rendering + pushing
# --------------------------------------------------------------------------

def service_label(service_no, minutes_by_no):
    mins = minutes_by_no.get(service_no)
    if mins is None:
        return f"{service_no} --"
    if mins == 0:
        return f"{service_no} ARR"
    return f"{service_no} {mins}MIN"


def build_lines(minutes_by_no):
    return [service_label(no, minutes_by_no) for no in CONFIG["SERVICES"]]


def push_awtrix(ip, lines):
    url = f"http://{ip}/api/v1/apps/pushed/{_app_path()}"
    payload = {
        "text": "  ".join(lines),
        "icon": AWTRIX_ICON_NAME,
        "durationMs": CONFIG["AWTRIX_DURATION_MS"],
        "textCase": "asTyped",
    }
    r = requests.put(url, json=payload, timeout=CLOCK_TIMEOUT)
    r.raise_for_status()


def clear_awtrix(ip):
    url = f"http://{ip}/api/v1/apps/{_app_path()}"
    r = requests.delete(url, timeout=CLOCK_TIMEOUT)
    if r.status_code == 404:  # app not there -> already cleared
        return
    r.raise_for_status()


def tc002_regions(lines, icon_b64):
    # The 52x16 panel would draw a plain text/icon payload at double size on a
    # 26x8 grid, so send a layout: icon in the left 16x16 box, one line per
    # service to its right. A line too long for its box scrolls through it.
    # Without an icon the text gets the full panel width.
    regions = []
    x, width = 0, 52
    if icon_b64:
        regions.append({"id": "icon", "box": [0, 0, 16, 16], "icon": f"data:image/gif;base64,{icon_b64}"})
        x, width = 18, 34
    for i, line in enumerate(lines[:2]):
        regions.append({"id": f"line{i + 1}", "box": [x, 8 * i, width, 8], "text": line, "color": "#FFFFFF", "align": "start"})
    return regions


def push_awtrix_tc002(ip, lines):
    url = f"http://{ip}/api/v1/apps/pushed/{_app_path()}"
    regions = tc002_regions(lines, tc002_icon_b64())
    payload = {"durationMs": CONFIG["TC002_DURATION"] * 1000, "layout": {"version": 1, "regions": regions}}
    r = requests.put(url, json=payload, timeout=CLOCK_TIMEOUT)
    r.raise_for_status()


def push_ulanzi_tc002(ip, lines):
    url = f"http://{ip}/api/custom"
    icon = tc002_icon_b64()
    # icon fills the leftmost 16px column (full panel height); text starts after it
    x = 18 if icon else 1
    text = [{"content": line, "fontHeight": 5, "x": x, "y": 1 + 8 * i, "color": "#FFFFFF"} for i, line in enumerate(lines[:2])]
    payload = {"duration": CONFIG["TC002_DURATION"], "text": text}
    if icon:
        payload["image"] = [{"data": f"data:image/gif;base64,{icon}", "position": [0, 0]}]
    r = requests.post(url, params={"name": CONFIG["APP_NAME"]}, json=payload, timeout=CLOCK_TIMEOUT)
    r.raise_for_status()


def clear_ulanzi_tc002(ip):
    url = f"http://{ip}/api/custom"
    r = requests.post(url, params={"name": CONFIG["APP_NAME"]}, json={}, timeout=CLOCK_TIMEOUT)
    r.raise_for_status()


PUSHERS = {"awtrix": push_awtrix, "awtrix_tc002": push_awtrix_tc002, "ulanzi_tc002": push_ulanzi_tc002}
CLEARERS = {"awtrix": clear_awtrix, "awtrix_tc002": clear_awtrix, "ulanzi_tc002": clear_ulanzi_tc002}


def _on_all_clocks(verb, handlers, *args):
    """Run handlers[kind](ip, *args) for every clock in parallel.

    Returns the number of clocks that succeeded; a slow or dead clock costs
    at most CLOCK_TIMEOUT and never blocks the others.
    """
    def one(item):
        ip, kind = item
        try:
            handlers[kind](ip, *args)
            return True
        except Exception as e:  # noqa: BLE001 - one bad clock must not stop the rest
            warn(f"{verb} on {ip} ({kind}) failed: {e}")
            return False

    if not CLOCKS:
        return 0
    with ThreadPoolExecutor(max_workers=len(CLOCKS)) as pool:
        return sum(pool.map(one, CLOCKS.items()))


def push_all(lines):
    return _on_all_clocks("push", PUSHERS, lines)


def clear_all():
    return _on_all_clocks("clear", CLEARERS)


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

def run_once():
    try:
        minutes_by_no, source = fetch_minutes(CONFIG["BUS_STOP_CODE"])
    except (requests.RequestException, FetchError) as e:
        warn(f"fetch failed: {e}")
        return

    # No data for any service (e.g. overnight when neither 102 nor 136
    # is running) -> remove the app instead of showing a blank "-- / --".
    if all(minutes_by_no.get(no) is None for no in CONFIG["SERVICES"]):
        ok = clear_all()
        log(f"no scheduled buses (via {source}) - cleared {ok}/{len(CLOCKS)} clocks")
        return

    lines = build_lines(minutes_by_no)
    ok = push_all(lines)
    log(f"pushed via {source} to {ok}/{len(CLOCKS)} clocks: {' | '.join(lines)}")


def _raise_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt


def poll_forever():
    quiet = False
    next_tick = time.monotonic()
    while True:
        if in_quiet_hours(CONFIG["QUIET_HOURS"], datetime.now()):
            if not quiet:
                clear_all()
                log("quiet hours - clocks cleared, pausing fetches")
                quiet = True
        else:
            if quiet:
                log("quiet hours over - resuming")
                quiet = False
            try:
                run_once()
            except Exception as e:  # noqa: BLE001 - keep the daemon alive no matter what
                warn(f"unexpected error in poll cycle: {e!r}")

        # Fixed-rate schedule on a monotonic clock: no drift, and if a cycle
        # overruns we skip the missed ticks instead of firing back-to-back.
        next_tick += CONFIG["POLL_SECONDS"]
        now = time.monotonic()
        if next_tick < now:
            next_tick = now
        time.sleep(next_tick - now)


def main():
    configure()
    if "--clear" in sys.argv:
        ok = clear_all()
        log(f"cleared {ok}/{len(CLOCKS)} clocks")
        return
    ensure_awtrix_icon()
    if "--once" in sys.argv:
        run_once()
        return
    # launchd / pm2 stop us with SIGTERM: treat it like Ctrl-C so the
    # clocks get cleared instead of being left showing stale times.
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    log(
        f"Polling stop {CONFIG['BUS_STOP_CODE']} for services "
        f"{CONFIG['SERVICES']} every {CONFIG['POLL_SECONDS']}s -> {list(CLOCKS)}"
    )
    try:
        poll_forever()
    except KeyboardInterrupt:
        log("stopping, clearing clocks...")
        clear_all()


if __name__ == "__main__":
    main()
