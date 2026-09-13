# The bus stop that lives on my wall now

Every weekday morning, the same ritual: shoes half on, bag half zipped, and one hand fumbling for my phone to open MyTransport just to answer one question — *do I have four minutes or fourteen?*

It's a stupid amount of friction for something so simple. The information exists, updates every 20 seconds, and I have two pixel clocks sitting around my apartment that spend most of their day telling me the time I already know from my phone. So I asked the obvious question: why am I unlocking a phone for this at all?

This is the story of how the bus stop outside my block ended up living on my wall — and the two things that almost derailed it.

## The bus stop

I live near Blk 477A, Upper Serangoon Road. Two services matter to me most mornings: **102** and **136**. A bit of digging turned up the actual bus stop code — **64409** — and from there the real question wasn't "can I get this data," it was "*whose* data is it, and who do I ask for it?"

## Does SMRT have an API for this?

First instinct: SMRT runs a chunk of the buses I take, so maybe they have a developer API. They don't — not a public one, anyway. What they have is **SMRTConnect**, a consumer app, closed off like every other transit app.

The actual source of truth turned out to be bigger than any one operator: **LTA DataMall**, Singapore's Land Transport Authority open data platform. Their Bus Arrival API (currently v3) doesn't care who runs the bus — SBS Transit, SMRT, Go-Ahead, Tower Transit, all of it comes through the same endpoint, keyed by bus stop code. Every bus app on your phone, SMRT's included, is ultimately a nicer UI wrapped around this same feed.

That's a good realization to have early: **build against the data, not against the brand.**

Getting a key was a five-minute form on datamall.lta.gov.sg — free account, a short paragraph on what you're building it for ("personal home-automation display, low-frequency polling, no redistribution"), and an `AccountKey` lands in your inbox. But registering, waiting for approval, and reading through a 6.9-version-deep PDF spec isn't a five-minute job, and I didn't want to stall the whole project on paperwork.

That's where **[BusRouter SG](https://busrouter.sg)** — and its underlying data proxy, [`arrivelah2.busrouter.sg`](https://github.com/cheeaun/arrivelah) — earned its keep. It's a long-running community project (the same one behind the excellent busrouter.sg live map) that sits in front of the official LTA feed and re-serves it with **zero registration, zero API key, just a bus stop code in the URL**:

```
GET https://arrivelah2.busrouter.sg/?id=64409
```

That let me build and verify the *entire* rest of the pipeline — bus-stop lookup, payload shaping, both clocks' push logic — same day, before my own LTA key had even arrived. It's what let me confirm, within minutes of starting, that stop 64409 genuinely carries both service 102 and 136 in real time, with live `duration_ms` countdowns I could sanity-check by eye against my own phone.

Once the official key came through, I flipped the primary fetch over to LTA DataMall directly — better to depend on the source than a third party for something now running unattended every two minutes. But busrouter.sg didn't get dropped; it got promoted to **automatic fallback**. If the direct LTA call ever fails — bad key, LTA having a bad day, network hiccup — the script (and the n8n workflow) silently retries against busrouter.sg instead of just going dark. Belt and suspenders, and a genuinely nice piece of community infrastructure doing quiet work in the background either way.

```
GET https://datamall2.mytransport.sg/ltaodataservice/v3/BusArrival?BusStopCode=64409
Header: AccountKey: <your key>
```

One call, both my services come back, each with the next three buses' estimated arrival times down to the second.

## The plot twist: two clocks, two different brains

I have two Ulanzi pixel clocks I wanted this running on. Same brand, same-looking hardware, sitting three feet apart on the same shelf. I assumed — reasonably, I thought — that pushing to one would look identical to pushing to the other.

It did not.

A quick `curl` against each one's IP told a different story:

- One clock answered with an AWTRIX-NG signature — the actively-maintained community firmware, the same one running the flight-tracker display I'd built for this shelf months earlier.
- The other answered as a **TC002**, Ulanzi's native firmware, with a completely different HTTP surface.

Two clocks, two APIs, two payload shapes:

| Clock | Firmware | How you push to it |
|---|---|---|
| Clock A | AWTRIX NG | `PUT /api/v1/apps/pushed/<name>` with `{"text", "durationMs", "textCase"}`, `DELETE` to clear |
| Clock B | TC002 native | `POST /api/custom?name=<name>` with `{"duration", "text": [...]}`, empty `{}` body to clear |

Nothing about either device's exterior would have told me this. The lesson, again: **probe before you build.** A five-minute `curl` against both devices saved me from writing one integration, watching it silently fail on the second clock, and burning an hour wondering why.

Once I knew the shapes, the fix was simple — one function that turns "bus 102 is 4 minutes away" into two different JSON bodies, one per clock, and fires them both.

## What it actually looks like

Two lines, one per bus:

```
102 4MIN
136 18MIN
```

`NOW` when a bus is pulling in, `--` on the rare occasion a service drops out of the feed. Nothing clever — just the number I actually need, sitting on the wall, updating on its own.

## From script to "set it and forget it"

The first version was a plain Python script: fetch, format, push, sleep, repeat. It worked, but it needed *me* to keep a terminal window open, which defeats half the point.

So it moved into n8n, which already runs the aircraft-tracker workflow on the same shelf. The whole pipeline became five nodes:

1. **Schedule Trigger** — every 2 minutes (LTA's feed refreshes every 20 seconds, but a display doesn't need to be that jumpy)
2. **Fetch from LTA DataMall** — with the account key, and an error branch wired straight into...
3. **...the arrivelah2.busrouter.sg fallback**, so one bad LTA response doesn't blank the display
4. **Build the two "next bus" lines**
5. **Push to both clocks**, each in its own dialect

I tested it by hand a few times, then flipped it active and watched the execution log for the next few cycles — every run green, both clocks updating, no babysitting required. It's been running since, quietly doing the one job I used to reach for my phone to do.

## The actual takeaway

None of the individual pieces here were hard. A REST API call. A JSON payload. A cron-style trigger. What made this worth writing up is the two habits that kept it from becoming a debugging afternoon:

1. **Chase the data to its source.** SMRT's app is a consumer product; LTA DataMall is the actual API. One question — "who really owns this data?" — saved me from building against a moving target.
2. **Never trust that "looks the same" means "is the same."** Two identical-looking clocks, two incompatible APIs. A thirty-second `curl` before writing a line of integration code turned a silent failure into a known constraint.

The bus is still going to be four minutes away or fourteen. But now I know which, without unlocking my phone — and that's the entire point of putting a screen on the wall in the first place.
