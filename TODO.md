# TODO

From the 2026-11 security/perf review (DeepSeek), triaged against the code.
Everything that can be done in the repo is done. What's left needs you.

## Open: needs you

- [ ] **Rotate the LTA AccountKey and the n8n API JWT.** Both were exposed to
      third-party model sessions, and the LTA key was also printed once in
      a Claude session. After rotating, update `AccountKey` / `n8n` in `.env`.
- [ ] **After rotating the LTA key,** update the n8n credential
      *LTA DataMall AccountKey* (Header Auth) with the new value. The
      workflow reads the key only from there now.
- [ ] **Network:** confirm the clocks' VLAN (`10.0.20.0/24`) only accepts
      port 80/7001 traffic from the feeder/n8n host (see README "Network").
- [ ] *(optional)* Nothing in this repo uses `SDKAccountKey`, `n8nurl` or
      `n8n` in `.env`. The `add-airline-icons` skill reads its own copies
      from the Ulanzi Feeder repo. `n8nurl`/`n8n` were used once, to deploy
      the workflow on 2026-11-07. Delete them, or keep them for future deploys.

## Done

- [x] `.env` is now `chmod 600`.
- [x] Deployed the hardened workflow to the live n8n workflow
      `zVyRwmxLbBsNyKU8` on 2026-11-07. Changes: credential
      *LTA DataMall AccountKey* added, key removed from the Config node,
      `availableInMCP: false`, and a new version published.
- [x] The poll loop survives any error: malformed rows are skipped, and
      there's a broad catch around each cycle.
- [x] An LTA response without a `Services` list now falls back to arrivelah
      (Python and n8n) instead of clearing the clocks.
- [x] `SERVICES` / `CLOCKS` entries are stripped. All config is validated
      at startup: stop code, services, app name, clock hosts, ints, quiet
      hours. Path segments are URL-encoded.
- [x] The TC002 icon loads lazily. If it's missing, the push goes out with
      no icon and the full-width text layout.
- [x] Clear calls check the HTTP status (404 counts as already cleared).
- [x] Logs are timestamped and flushed. SIGTERM clears the clocks, the same
      as Ctrl-C.
- [x] n8n: LTA key removed from the Config node (credential instead),
      timeouts on every HTTP node, a retry on the fetches, a swallowed error
      output on the arrivelah fetch, non-fatal clears, `encodeURIComponent`
      in URLs. Local export: explicit `secondsInterval: 30`,
      `availableInMCP: false`.
- [x] Pooled `requests.Session` with a single retry for the APIs, parallel
      clock pushes with a 3 s timeout, a fixed-rate monotonic schedule, and
      opt-in `QUIET_HOURS`.
- [x] Pinned `requirements.txt` and `requirements-dev.txt`. Added ruff
      config, a pytest suite, and GitHub Actions CI (ruff, pytest,
      pip-audit, plus a no-secrets check on the n8n templates).

## Declined

- **The "secondsInterval missing = 1 s polling" claim (P1).** n8n leaves
  parameters at their default out of exports, and the default is 30. It's
  now set explicitly anyway.
