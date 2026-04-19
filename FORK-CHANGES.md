# Fork Changes (`woobins/mktxp@homelab-patches`)

This branch carries homelab-specific patches on top of upstream
`akpw/mktxp`. Consumed by the Ansible role at
[`homelab-infra/ansible/roles/mktxp`](https://github.com/woobins/homelab-infra/tree/main/ansible/roles/mktxp),
which pins a specific commit SHA and installs via `pip install` from a
GitHub archive URL.

## Why a fork

The changes here are either (a) features upstream hasn't added yet, or
(b) fixes for RouterOS 7's `wifi` (wifi-qcom) package that upstream
hadn't caught up to when we needed them. Nothing here is secret or
homelab-unique in spirit — most could be upstreamed if/when someone has
time. Until then, carrying a small patch set is cheaper than waiting.

## How it's consumed

```yaml
# homelab-infra/ansible/roles/mktxp/defaults/main.yml
mktxp_source_commit: "<sha>"
mktxp_source_url: "https://github.com/woobins/mktxp/archive/{{ mktxp_source_commit }}.tar.gz"
```

Bumping the fork:

1. Commit to `homelab-patches` and push
2. `git -C /mnt/workspace/projects/mktxp rev-parse HEAD` → new SHA
3. Update `mktxp_source_commit` in the role's `defaults/main.yml`
4. `ansible-playbook deploy-mktxp.yml -e mktxp_force_reinstall=true`

`-e mktxp_force_reinstall=true` must be passed as a CLI extra-var on the
first deploy after a SHA change so pip actually reinstalls the venv
(without it, pip sees the package is already installed and skips).

## Patches currently on this branch

Branch point: upstream `c4e3da5` ("finish #308 with dedicated bridge
VLAN collector"). Five patches on top:

### 1. `0d544d6` — CAPsMAN interfaces datasource: add `wifi` package support

Upstream's `CapsmanInterfacesDatasource` only knew about the legacy
`wireless` package. RouterOS 7 with the `wifi` (wifi-qcom) package
exposes CAPsMAN virtual interfaces under `/interface/wifi` with names
prefixed `cap-wifi*`. This patch teaches the datasource to read them
from the new path when the router advertises the wifi/wifiwave2 package,
and to fall back to the legacy `/caps-man/interface` path otherwise. Also
extracts a primary `frequency` from the `channel.frequency` field (which
on new package is comma-separated for multi-channel/DFS configs).

**Files:** `mktxp/datasource/capsman_ds.py`

### 2. `0d544d6` (same commit) — `configuration` label on `capsman_clients_*` metrics

The `interface` label (cap-wifiN) is **dynamic** — CAPsMAN renumbers
virtual interfaces based on CAP-reconnect order after a router or CAP
reboot. Any Grafana dashboard or alert keyed on `interface="cap-wifi3"`
breaks the next time an AP comes back in a different order. The
`configuration` name (e.g. `cfg-5GHZ-dacave`) is user-defined, stable
across reboots, and meaningful ("which physical AP, which band"). This
patch plumbs the interface → configuration map through
`CapsmanCollector.collect()` so every registration record gets a
`configuration` label alongside `interface`.

**Files:** `mktxp/collector/capsman_collector.py`,
`mktxp/datasource/capsman_ds.py`

### 3. `8465b53` — `frequency` label on `capsman_clients_*` and `capsman_interfaces`

Companion to #2. Adds the current operating frequency (in MHz, as a
string) as a label. Lets dashboards group clients and channel-congestion
metrics by operating channel without having to hard-code
channel-to-AP mappings. Uses the same channel-info map as the
configuration-label work — single API call, two labels emitted.

**Files:** `mktxp/collector/capsman_collector.py`,
`mktxp/datasource/capsman_ds.py`

### 4. `56cacb9` — `wifi_neighbor` collector: neighbor APs + client signal via flat-snoop  ⚠️ DISABLED IN PROD

**Current status (2026-04-19): feature-complete but DISABLED** in
`homelab-infra/ansible/roles/mktxp/templates/mktxp.conf.j2`
(`wifi_neighbor = False`). Root cause: every flat-snoop call on a
CAPsMAN-bound `cap-wifiN` interface causes the corresponding physical
AP to stop and restart its SSID for the full snoop duration, dropping
all connected clients. Not a code bug — intrinsic CAPsMAN behavior.
See `homelab-infra/incidents/2026-04-19-0700-wifi-neighbor-ap-restart.md`
for post-mortem and remediation options.



New collector that uses `/interface/wifi/flat-snoop` (wifi-qcom only) on
CAPsMAN-managed radios to surface two things per scrape:

- **Neighbor APs** (BSSes visible on/near our channel) — with signal,
  SNR, noise, frame count, BSS uptime, security details
- **Passively-observed clients** (STAs emitting probe requests or
  ambient traffic) — same client can be seen from multiple APs
  simultaneously, enabling triangulation and sticky-client detection

One radio is snooped per scrape (round-robin), bounded at a config-
controlled duration (default 7s). The wifi-qcom driver channel-hops
opportunistically during the snoop window rather than parking
off-channel, so connected clients are not disconnected — verified
empirically up to ~10s. Longer durations (~15s+) start causing client
beacon-miss and roaming.

Both filter types (`bsss` for APs + `stas` for clients) come from a
single snoop call (`filter-type=bsss,stas`), so no extra scrape cost
vs neighbor-only.

Config keys added:

- `wifi_neighbor` (bool, default off): feature flag
- `wifi_neighbor_scan_duration` (str int, default 7): seconds per snoop
- `wifi_neighbor_allowlist` (comma-separated MACs, default None): BSSIDs
  NOT in this list get flagged via `mktxp_wifi_neighbor_rogue_info`

Metrics emitted — see the collector source for the full list; headline
items:

- `mktxp_wifi_neighbor_signal_dbm`, `_snr_db`, `_noise_dbm`,
  `_seen_frames`, `_bss_on_time_seconds`, `_last_seen_seconds`,
  `_rate_mbps`, `_count`, `_rogue_info`, `_info`
- `mktxp_wifi_client_signal_dbm`, `_snr_db`, `_noise_dbm`,
  `_seen_frames`, `_last_seen_seconds`, `_rate_mbps`, `_count`

**Files:**
`mktxp/datasource/wifi_neighbor_ds.py` (new),
`mktxp/collector/wifi_neighbor_collector.py` (new),
`mktxp/cli/config/config.py` (keys + namedtuple fields),
`mktxp/flow/collector_registry.py` (registration)

### 5. `d400133` — `wifi_neighbor`: drop `rate_mbps` from label sets

Bug-fix for #4. `rate_mbps` is coerced to float in the datasource (for
the `_rate_mbps` gauge), but was also listed in several label sets.
Prometheus rejects non-string label values during serialization, so
`/metrics` crashed with
`AttributeError: 'float' object has no attribute 'replace'`.
Local tests iterating `mf.samples` missed it; the bug only appears when
`generate_latest()` is called. Fix removes `rate_mbps` from label lists
— no data loss (the gauge metric still exposes it).

**Lesson:** new-collector local tests should call
`prometheus_client.generate_latest()` on the built registry to exercise
the serialization path, not just inspect `mf.samples`.

**Files:** `mktxp/collector/wifi_neighbor_collector.py`

### 6. `15a5390` — `wifi_neighbor`: register in `RouterEntry.time_spent` init

Bug-fix for #4. `collector_handler` does
`router_entry.time_spent[collector_ID] += ...` per collect call, which
raises `KeyError` for any collector not pre-populated in `time_spent`.
Every collector listed in `CollectorRegistry` must *also* be initialized
to `0` in `RouterEntry.time_spent`'s init dict. Missed in the original
commit; manifested as
`Exception while scraping <router>: 'WifiNeighborCollector'` on every
scrape (non-fatal — metrics still flowed since the error was caught
after the yield — but the log was noisy and per-collector duration
accounting was broken).

**If you add a new collector in the future:** register it in BOTH
`mktxp/flow/collector_registry.py` (for dispatch) AND
`mktxp/flow/router_entry.py` (for `time_spent` init).

**Files:** `mktxp/flow/router_entry.py`

## Rebase risk areas

Next upstream bump, conflicts are most likely in these files:

| File | Why |
|---|---|
| `mktxp/datasource/capsman_ds.py` | Heavy local changes; upstream may refactor CAPsMAN handling |
| `mktxp/collector/capsman_collector.py` | Label set extension — any upstream label changes will conflict |
| `mktxp/cli/config/config.py` | Three sections touched: keys, STR_KEYS, namedtuple, default-value dispatch |
| `mktxp/flow/collector_registry.py` | Trivial add; unlikely to conflict |
| `mktxp/flow/router_entry.py` | One-line time_spent addition; low risk |

New files (`mktxp/datasource/wifi_neighbor_ds.py`,
`mktxp/collector/wifi_neighbor_collector.py`) won't conflict by
themselves.

## Upstream upstreaming candidates

If we ever want to reduce the patch set:

- Patches #1–#3 (CAPsMAN wifi-package support + stable labels) are
  general-purpose and could be a PR. The `configuration` label in
  particular addresses a real bug for anyone running CAPsMAN on
  RouterOS 7.
- Patch #4 (`wifi_neighbor` collector) is more opinionated (requires
  the `wifi` package; uses flat-snoop which is wifi-qcom-only) so
  might not be upstream-friendly without a lot of polish.

## Not yet implemented (but discussed)

- **Tier 2 / full-sweep collector** — a separate Python script + systemd
  timer that runs a longer (15s) flat-snoop against each radio once
  daily, writes results via textfile collector. Would surface the
  weak-neighbor tail that Tier 1's 7s snoop misses. Skipped for now
  because Tier 1 covers the primary use cases (rogue detection, strong-
  neighbor visibility, same-client-from-multiple-APs triangulation).
  Re-evaluate if a specific dashboard need appears.
