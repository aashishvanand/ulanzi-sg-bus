#!/usr/bin/env python3
"""Push next-bus arrival times for two SG bus services to two Ulanzi clocks.

Data source: LTA DataMall's Bus Arrival API v3 (the source LTA operators'
apps, including SMRT's, ultimately pull from), called directly with an
AccountKey from .env. Falls back automatically to arrivelah2.busrouter.sg
(a free, no-key proxy in front of the same API) if the direct call fails
for any reason.

The two target clocks run DIFFERENT firmware and need different push APIs:
  - 10.0.20.30 -> AWTRIX NG   (PUT  /api/v1/apps/pushed/<name>, DELETE to clear)
  - 10.0.20.31 -> TC002 native (POST /api/custom?name=<name>, {} body to clear)
Both were probed live against the real devices to confirm this (see README.md).
"""
import base64
import os
import sys
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

CONFIG = {
    "BUS_STOP_CODE": os.environ.get("BUS_STOP_CODE", "64409"),  # Blk 477A Upp Serangoon Rd
    "SERVICES": os.environ.get("SERVICES", "102,136").split(","),
    "APP_NAME": os.environ.get("APP_NAME", "sgbus"),
    "POLL_SECONDS": int(os.environ.get("POLL_SECONDS", "30")),
    "AWTRIX_DURATION_MS": int(os.environ.get("AWTRIX_DURATION_MS", "8000")),
    "TC002_DURATION": int(os.environ.get("TC002_DURATION", "10")),
    "LTA_ACCOUNT_KEY": os.environ.get("AccountKey"),
}

# ip -> firmware ("awtrix" or "tc002")
CLOCKS = {
    "10.0.20.30": "awtrix",
    "10.0.20.31": "tc002",
}

LTA_BUS_ARRIVAL_URL = "https://datamall2.mytransport.sg/ltaodataservice/v3/BusArrival"
ARRIVELAH_URL = "https://arrivelah2.busrouter.sg/"

ICONS_DIR = os.path.join(os.path.dirname(__file__), "icons")
# Generated from icon.jpeg (the Singapore Public Transport symbol) via
# generate_transport_icon.py. Neither the source image nor these two
# generated files are committed to git — trademarked/non-free mark, see
# README.md. Regenerate locally from your own copy of icon.jpeg if missing.
AWTRIX_ICON_NAME = "sgpt_wave"  # uploaded once, referenced by ID thereafter
# ^ AWTRIX NG appears to cache a loaded icon's bitmap in memory keyed by
# this name, independent of the file's on-disk content — replacing the
# file under the same name did NOT refresh what was shown on the panel.
# Bumping the id forces it to load fresh. If you ever regenerate this
# icon again, bump the name again (or power-cycle the clock) rather than
# just re-uploading under the same name.
TC002_ICON_PATH = os.path.join(ICONS_DIR, "transport_icon_16.gif")

with open(TC002_ICON_PATH, "rb") as _f:
    TC002_ICON_B64 = base64.b64encode(_f.read()).decode()


def ensure_awtrix_icon():
    """Upload the transport-symbol icon to the AWTRIX clock if not there yet."""
    ip = next((ip for ip, fw in CLOCKS.items() if fw == "awtrix"), None)
    if not ip:
        return
    try:
        r = requests.get(f"http://{ip}/api/v1/files", params={"dir": "/ICONS"}, timeout=5)
        r.raise_for_status()
        names = {f["name"] for f in r.json().get("files", [])}
        if f"{AWTRIX_ICON_NAME}.gif" in names:
            return
        icon_path = os.path.join(ICONS_DIR, "transport_icon_8.gif")
        with open(icon_path, "rb") as f:
            up = requests.post(
                f"http://{ip}/api/v1/files",
                params={"dir": "/ICONS"},
                files={"file": (f"{AWTRIX_ICON_NAME}.gif", f, "image/gif")},
                timeout=10,
            )
        up.raise_for_status()
        print(f"uploaded {AWTRIX_ICON_NAME}.gif to AWTRIX clock ({ip})")
    except (requests.RequestException, FileNotFoundError) as e:
        print(f"[warn] could not ensure AWTRIX icon: {e}", file=sys.stderr)


def minutes_until_ms(duration_ms):
    if duration_ms is None:
        return None
    return max(0, round(duration_ms / 60000))


def minutes_until_iso(iso_str):
    if not iso_str:
        return None
    try:
        arrival = datetime.fromisoformat(iso_str)
    except ValueError:
        return None
    now = datetime.now(arrival.tzinfo)
    return max(0, round((arrival - now).total_seconds() / 60))


def fetch_lta_minutes(bus_stop_code, account_key):
    r = requests.get(
        LTA_BUS_ARRIVAL_URL,
        params={"BusStopCode": bus_stop_code},
        headers={"AccountKey": account_key, "accept": "application/json"},
        timeout=8,
    )
    r.raise_for_status()
    minutes_by_no = {}
    for svc in r.json().get("Services", []):
        arrival_iso = svc.get("NextBus", {}).get("EstimatedArrival")
        minutes_by_no[svc["ServiceNo"]] = minutes_until_iso(arrival_iso)
    return minutes_by_no


def fetch_arrivelah_minutes(bus_stop_code):
    r = requests.get(ARRIVELAH_URL, params={"id": bus_stop_code}, timeout=10)
    r.raise_for_status()
    minutes_by_no = {}
    for svc in r.json().get("services", []):
        minutes_by_no[svc["no"]] = minutes_until_ms(svc.get("next", {}).get("duration_ms"))
    return minutes_by_no


def fetch_minutes(bus_stop_code):
    if CONFIG["LTA_ACCOUNT_KEY"]:
        try:
            return fetch_lta_minutes(bus_stop_code, CONFIG["LTA_ACCOUNT_KEY"]), "lta"
        except requests.RequestException as e:
            print(f"[warn] LTA DataMall fetch failed ({e}), falling back to arrivelah", file=sys.stderr)
    return fetch_arrivelah_minutes(bus_stop_code), "arrivelah"


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
    url = f"http://{ip}/api/v1/apps/pushed/{CONFIG['APP_NAME']}"
    payload = {
        "text": "  ".join(lines),
        "icon": AWTRIX_ICON_NAME,
        "durationMs": CONFIG["AWTRIX_DURATION_MS"],
        "textCase": "asTyped",
    }
    r = requests.put(url, json=payload, timeout=5)
    r.raise_for_status()


def clear_awtrix(ip):
    url = f"http://{ip}/api/v1/apps/{CONFIG['APP_NAME']}"
    requests.delete(url, timeout=5)


def push_tc002(ip, lines):
    url = f"http://{ip}/api/custom"
    # icon fills the leftmost 16px column (full panel height); text starts after it
    text = [
        {"content": lines[0], "fontHeight": 5, "x": 18, "y": 1, "color": "#FFFFFF"},
    ]
    if len(lines) > 1:
        text.append({"content": lines[1], "fontHeight": 5, "x": 18, "y": 9, "color": "#FFFFFF"})
    payload = {
        "duration": CONFIG["TC002_DURATION"],
        "text": text,
        "image": [{"data": f"data:image/gif;base64,{TC002_ICON_B64}", "position": [0, 0]}],
    }
    r = requests.post(url, params={"name": CONFIG["APP_NAME"]}, json=payload, timeout=5)
    r.raise_for_status()


def clear_tc002(ip):
    url = f"http://{ip}/api/custom"
    requests.post(url, params={"name": CONFIG["APP_NAME"]}, json={}, timeout=5)


PUSHERS = {"awtrix": push_awtrix, "tc002": push_tc002}
CLEARERS = {"awtrix": clear_awtrix, "tc002": clear_tc002}


def push_all(lines):
    for ip, firmware in CLOCKS.items():
        try:
            PUSHERS[firmware](ip, lines)
        except requests.RequestException as e:
            print(f"[warn] push to {ip} ({firmware}) failed: {e}", file=sys.stderr)


def clear_all():
    for ip, firmware in CLOCKS.items():
        try:
            CLEARERS[firmware](ip)
        except requests.RequestException as e:
            print(f"[warn] clear on {ip} ({firmware}) failed: {e}", file=sys.stderr)


def run_once():
    try:
        minutes_by_no, source = fetch_minutes(CONFIG["BUS_STOP_CODE"])
    except requests.RequestException as e:
        print(f"[warn] fetch failed: {e}", file=sys.stderr)
        return

    # No data for either service (e.g. overnight when neither 102 nor 136
    # is running) -> remove the app instead of showing a blank "-- / --".
    if all(minutes_by_no.get(no) is None for no in CONFIG["SERVICES"]):
        clear_all()
        print(f"no scheduled buses (via {source}) — cleared both clocks")
        return

    lines = build_lines(minutes_by_no)
    push_all(lines)
    print(f"pushed via {source}: {' | '.join(lines)}")


def main():
    if "--clear" in sys.argv:
        clear_all()
        print("cleared both clocks")
        return
    ensure_awtrix_icon()
    if "--once" in sys.argv:
        run_once()
        return
    print(
        f"Polling stop {CONFIG['BUS_STOP_CODE']} for services "
        f"{CONFIG['SERVICES']} every {CONFIG['POLL_SECONDS']}s -> {list(CLOCKS)}"
    )
    try:
        while True:
            run_once()
            time.sleep(CONFIG["POLL_SECONDS"])
    except KeyboardInterrupt:
        print("\nstopping, clearing clocks...")
        clear_all()


if __name__ == "__main__":
    main()
