# mktxp — woobins fork

This is a **fork** of [`akpw/mktxp`](https://github.com/akpw/mktxp) on
the `homelab-patches` branch, consumed by pip install from a GitHub
archive URL by the Ansible role at
`homelab-infra/ansible/roles/mktxp`. See
[`FORK-CHANGES.md`](./FORK-CHANGES.md) for the full inventory of local
patches, rationale, and rebase risk per file.

## Before editing

- Read `FORK-CHANGES.md` so you know which files are already heavily
  patched (CAPsMAN datasource + collector, config keys, router_entry).
  Changes there have elevated merge-conflict risk on the next upstream
  bump.
- Upstream merge-base is tracked; check `git merge-base upstream/main
  origin/homelab-patches` if you need to diff our patches against
  stock.

## Traps to avoid (learned the hard way — don't repeat)

### 1. Test with `generate_latest()`, not just `mf.samples`

When adding a new collector, a local test that iterates
`for mf in Collector.collect(re): list(mf.samples)` will **miss**
Prometheus serialization errors — specifically, non-string label values
(floats, ints) crash only when the text encoder runs. Always include:

```python
from prometheus_client import CollectorRegistry
from prometheus_client.exposition import generate_latest
reg = CollectorRegistry()
reg.register(some_proxy_that_collects(your_collector))
out = generate_latest(reg)  # raises if any label is non-string
```

Real bug this caught: `rate_mbps` was both a float gauge value AND in
the label set. Local test passed. Production crashed `/metrics` with
`AttributeError: 'float' object has no attribute 'replace'`.

### 2. New collectors must register in TWO places

- `mktxp/flow/collector_registry.py` — for dispatch (obvious)
- `mktxp/flow/router_entry.py` — add to the `time_spent` init dict
  (less obvious)

`collector_handler` does
`router_entry.time_spent[collector_ID] += ...` after each collect call.
If the key isn't pre-populated, `KeyError` is raised for every router
on every scrape. Metrics still flow (error is caught after the yield),
but the log fills up and duration accounting is wrong.

### 3. wifi_neighbor collector is disabled — CAPsMAN flat-snoop restarts the AP

The `wifi_neighbor` collector is feature-complete but **disabled in
production** as of 2026-04-19. Root cause: `/interface/wifi flat-snoop`
issued via CAPsMAN against a `cap-wifiN` bound virtual interface causes
the corresponding physical AP to stop/start its SSID for the full snoop
duration, dropping all connected clients. Confirmed via rtr-panel syslog
`"stopping AP, disabling"` events correlating 1:1 with snoop starts.

Earlier lab tests ("non-disruptive at ≤10s") missed this because the
target client had already roamed off the snooped radio and ICMP kept
flowing via its new AP.

See `homelab-infra/incidents/2026-04-19-0700-wifi-neighbor-ap-restart.md`
for the full post-mortem and remediation options. If you're touching
this collector, start there.

### 4. `BaseDSProcessor.trimmed_records` STRIPS fields not in `metric_labels`

When the collector passes `metric_labels` to the datasource, any record
field NOT in that list gets dropped. If you later try to use a field as
a gauge value (e.g. `BaseCollector.gauge_collector(..., 'signal', labels)`),
the value is gone and the gauge silently defaults to `0`. Include every
field the collector needs (labels AND gauge value keys) in the
`metric_labels` arg to the datasource; pass a narrower list to each
`gauge_collector`/`info_collector` for its Prometheus label shape.

## Bump + deploy

1. Commit to `homelab-patches`, push
2. `git rev-parse HEAD` → new SHA
3. Update `mktxp_source_commit` in
   `homelab-infra/ansible/roles/mktxp/defaults/main.yml`
4. `ansible-playbook deploy-mktxp.yml -e mktxp_force_reinstall=true`

The `-e mktxp_force_reinstall=true` on the CLI must be passed as the
first deploy after a SHA change (otherwise pip sees the package is
already installed and skips). The role's `when:` condition uses
`| bool` so CLI strings parse correctly on ansible-core 2.19+.

## When adding a patch

1. Make the change on `homelab-patches`
2. **Update `FORK-CHANGES.md`** — new patches need an entry with:
   rationale, files touched, and rebase risk notes. That file is the
   authoritative changelog; commit messages rot but the markdown stays
   readable.
3. Consider whether the change is upstream-friendly. The more general-
   purpose patches (CAPsMAN wifi-package support, stable `configuration`
   label) are good PR candidates if we ever want to shrink the patch set.
