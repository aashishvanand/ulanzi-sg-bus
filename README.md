# Ulanzi SG Bus — next-bus display

Shows next-arrival times for bus services **102** and **136** at the bus
stop outside **Blk 477A, Upper Serangoon Road** (bus stop code `64409`,
confirmed live — both services appear in that stop's arrival data) on two
Ulanzi pixel clocks.

## Is there an SMRT app / public API for this?

- **SMRT itself** has an app (**SMRTConnect**) that shows real-time SMRT
  train/bus arrivals, but it's a consumer app with no public API — nothing
  to poll programmatically.
- **The actual public API** is **LTA DataMall**'s Bus Arrival API (v3),
  which covers *all* operators (SBST, SMRT, Go-Ahead, Tower Transit) for
  every bus stop in Singapore — SMRT doesn't run its own separate arrivals
  API.

## Data source

Fetches straight from LTA DataMall — the actual source, not a third-party
mirror:

```
GET https://datamall2.mytransport.sg/ltaodataservice/v3/BusArrival?BusStopCode=<code>
Header: AccountKey: <your key>
```

Confirmed live against stop `64409` (both services 102/SBST and 136/GAS
returned, with per-bus `EstimatedArrival` timestamps). Needs a free
`AccountKey` from [datamall.lta.gov.sg](https://datamall.lta.gov.sg),
stored in `.env` (gitignored) as:

```
AccountKey=xxxxxxxxxxxxxxxxxxxxxx==
```

(The `SDKAccountKey` from the *Extended OBU Library SDK* guide is a
different credential for ERP2.0 on-board-unit telematics — unrelated to
Bus Arrival, not used here.)

**Automatic fallback:** if the direct LTA call fails for any reason (key
revoked, LTA outage, network issue), the script falls back to
**[arrivelah2.busrouter.sg](https://github.com/cheeaun/arrivelah)**, a
free, no-key proxy in front of the same underlying API — no manual
intervention needed, it just logs a warning and keeps the display running
off the mirror until LTA is reachable again.

## The two clocks run different firmware

Both were probed live before writing the push logic — they are **not**
the same:

| IP | Firmware | Push API |
|---|---|---|
| `10.0.20.30` | AWTRIX NG (a TC001, same as the sibling `Ulanzi Feeder` repo) | `PUT /api/v1/apps/pushed/<name>` JSON `{"text", "durationMs", "textCase"}`, `DELETE /api/v1/apps/<name>` to clear |
| `10.0.20.31` | TC002 native firmware | `POST /api/custom?name=<name>` JSON `{"duration", "text":[...]}`, `POST` with `{}` body to clear |

`bus_feeder.py` builds one set of "next bus" lines and pushes each in the
format that clock's firmware expects. If you repoint this at clocks that
are both the same firmware, trim `CLOCKS`/`PUSHERS` accordingly.

## Usage

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt

venv/bin/python3 bus_feeder.py --once     # single push, then exit
venv/bin/python3 bus_feeder.py            # poll loop (every 30s by default)
venv/bin/python3 bus_feeder.py --clear    # remove the app from both clocks
```

Config is via environment variables (all optional, shown with defaults):

| Var | Default | Meaning |
|---|---|---|
| `BUS_STOP_CODE` | `64409` | Blk 477A Upp Serangoon Rd |
| `SERVICES` | `102,136` | comma-separated bus service numbers |
| `APP_NAME` | `sgbus` | app/slot name on both clocks |
| `POLL_SECONDS` | `30` | loop interval |
| `AWTRIX_DURATION_MS` | `8000` | how long the AWTRIX NG clock shows the app per cycle |
| `TC002_DURATION` | `10` | same, in seconds, for the TC002 clock |

Clock IPs and firmware mapping are in the `CLOCKS` dict at the top of
`bus_feeder.py` (not env-configurable, since the two firmwares need
different code paths anyway).

`AccountKey` is read from `.env` (via `python-dotenv`) — see **Data
source** above.

## Keeping it running

Two ways to run this on a schedule:

1. **`bus_feeder.py`'s own poll loop** — under `launchd`, `pm2`, or a
   `tmux`/`screen` session.
2. **n8n** (what's actually deployed): reimplements the exact same fetch
   → normalize → build-lines → push-to-both-clocks pipeline as JS Code
   nodes, on a **Schedule Trigger (every 30 seconds)**. It's published and
   **active** on the n8n instance at `n8nurl` (see `.env`) as workflow id
   `zVyRwmxLbBsNyKU8`, named **"SG Bus - Next Arrival (102/136)"** —
   verified end-to-end via the n8n API (both clock pushes returned
   success). LTA DataMall is the primary fetch node, with the same
   arrivelah2.busrouter.sg fallback wired in via the HTTP node's
   `onError: continueErrorOutput` branch.

   Two copies of the workflow file, same convention as the sibling repo's
   `n8n_aircraft_workflow(.local).json`:

   | File | `LTA_ACCOUNT_KEY` value | Committed to git? |
   |---|---|---|
   | `n8n_bus_workflow.json` | placeholder `PASTE_YOUR_LTA_ACCOUNTKEY` | yes — safe to publish |
   | `n8n_bus_workflow.local.json` | your real key, embedded | **no** — gitignored |

   Import `n8n_bus_workflow.local.json` if you want to re-deploy this
   exact workflow without manually re-pasting the key afterward; import
   `n8n_bus_workflow.json` (and paste your key into the **Config** node
   yourself) if you're publishing/sharing the repo. Regenerate the
   `.local.json` any time the public one changes by copying it and
   substituting your key in the `Config (edit per location)` node's
   `LTA_ACCOUNT_KEY` assignment. Change the interval on the **Every 30s**
   node if you want a different polling cadence.

### Why the clock sometimes reads a couple minutes off from MyTransport

The display shows a **static snapshot from the last push** — it doesn't
count down between refreshes the way a live app does. Right after a push
it's accurate; right before the *next* one, it's stale by up to one full
poll interval, and it's always stale in the same direction (showing
*more* time remaining than actually true, since real time has passed
since the last calculation). At the original 2-minute interval that meant
occasionally seeing "18MIN" on the wall when MyTransport (recalculating
live, in your hand, the instant you look) already said 16. Verified this
wasn't a math or data-source problem first — pulled `arrivelah`'s
[`server.js`](https://github.com/cheeaun/arrivelah/blob/master/server.js)
and confirmed it does a plain `new Date(EstimatedArrival) - now`, no
offset applied — and a fresh direct LTA fetch against stop `64409`
matched the expected minutes exactly at the moment it was fetched.

Fix: dropped the interval to **30 seconds** (LTA's own feed refreshes
every 20s, so this stays well within that), capping worst-case staleness
at half a minute instead of two. It can't be made perfectly live without
either polling every few seconds (unnecessary API load) or the clock
firmware supporting an actual countdown timer (neither AWTRIX NG nor
TC002 do).

## Display format

Two lines, e.g.:
```
102 4MIN
136 18MIN
```
`ARR` when a bus is arriving/at the stop (matches MyTransport's own "Arr"
wording), `--` when one service has no arrival data.

**When *both* services have no data (e.g. overnight when neither 102 nor
136 is running), the app is removed from both clocks entirely** rather
than sitting there showing a stale/blank `-- / --`. `bus_feeder.py`
checks this after every fetch (`run_once()`) and calls `clear_all()`
instead of pushing; the n8n workflow does the same via an **"Any Bus
Scheduled?"** IF node between **Build Lines** and the payload builders,
routing to a **Clear Clock (AWTRIX)** / **Clear Clock 2 (TC002)** branch
when neither service has data. It's checked per-cycle, so the app
reappears on its own the moment either service starts running again in
the morning.

## Icon

Both clocks show the Singapore Public Transport symbol next to the
arrival times, generated from a reference photo (`icon.jpeg`) via
`generate_transport_icon.py`:

- **AWTRIX (`.30`)**: uploaded once to the device's `/ICONS` folder and
  referenced by ID (`icon: "sgpt_wave"`) in every push — AWTRIX has a
  real icon-by-ID system, so `ensure_awtrix_icon()` only uploads it if
  it's missing.
- **TC002 (`.31`)**: has no icon-by-ID system, so the icon travels as an
  inline base64 GIF (`image: [...]`) in *every* push, per
  `TC002-PUSH-NOTES.md` in the sibling repo. Text is shifted to `x: 18` to
  leave room for the 16px-wide icon column.

**Not committed to git**: this mark is trademarked/non-free (see its own
[Wikimedia Commons licensing](https://commons.wikimedia.org/wiki/File:Singapore_Public_Transport_logo.jpg)),
used here only for a personal display. `icon.jpeg`,
`icons/transport_icon_8.gif`, and `icons/transport_icon_16.gif` are all
gitignored. If you clone this repo fresh, `bus_feeder.py` will fail on
the missing icon file until you drop in your own `icon.jpeg` and run
`python3 generate_transport_icon.py` — or edit `bus_feeder.py` to skip
the `icon`/`image` fields entirely and ship text-only, which is what the
public `n8n_bus_workflow.json` template does (no icon reference at all,
since there's no safe default to ship).

**Two different generation strategies, one per resolution** — the actual
logo is wide (~1.86:1) and detailed, and it turns out a single
algorithmic downscale doesn't work well at both sizes:

- **16x16** (TC002): crop to the mark's bounding box, fit it into the
  square *preserving aspect ratio* (letterboxed — squishing it to fill
  the square distorts the shape too much at this size), then
  hard-threshold back to exactly two flat colors (letting the quantizer
  pick from LANCZOS's anti-aliased edge tones produced a muddy result).
  This reads as a close match to the real mark.
- **8x8** (AWTRIX): every algorithmic attempt — squish or letterbox —
  reads as an unrecognizable scribble or collapses into a plain cross at
  this resolution; there just isn't enough detail budget for the real
  curves. So it's **hand-authored** instead (`ICON_8_GRID` in
  `generate_transport_icon.py`): a simple zigzag wave motif in the same
  color, chosen over three candidates for legibility rather than literal
  fidelity.

**Device-side icon caching, worth knowing if you ever regenerate one of
these**: replacing the icon *file* on the AWTRIX clock under the same
filename did not refresh what was actually shown on the panel — the
firmware appears to cache the decoded bitmap in memory keyed by the icon
name, independent of on-disk content. The fix each time was bumping the
icon id (`transport_icon` → `sgpt_mark` → `sgpt_wave`) rather than
re-uploading under the same name. If you regenerate this icon again,
bump `AWTRIX_ICON_NAME` (and the matching reference in
`n8n_bus_workflow.local.json`) again too.
