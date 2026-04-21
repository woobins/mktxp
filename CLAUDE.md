# mktxp — woobins fork

Fork of [`akpw/mktxp`](https://github.com/akpw/mktxp) on the `homelab-patches` branch. Installed via pip from a GitHub archive URL by the Ansible role at `homelab-infra/ansible/roles/mktxp`. See [`FORK-CHANGES.md`](./FORK-CHANGES.md) for the full patch inventory with rationale and rebase-risk notes per file.

## Before editing

- Read `FORK-CHANGES.md` — CAPsMAN datasource/collector, config keys, and `router_entry` are heavily patched with elevated merge-conflict risk.
- Upstream merge base: `git merge-base upstream/main origin/homelab-patches`

## Bump + deploy

1. Commit to `homelab-patches`, push
2. `git rev-parse HEAD` → new SHA
3. Update `mktxp_source_commit` in `homelab-infra/ansible/roles/mktxp/defaults/main.yml`
4. `ansible-playbook deploy-mktxp.yml -e mktxp_force_reinstall=true`

The `-e mktxp_force_reinstall=true` flag is required after a SHA change — without it pip skips reinstall if the package version string is unchanged. The role uses `| bool` so CLI strings parse correctly on ansible-core 2.19+.

## When adding a patch

1. Make the change on `homelab-patches`
2. **Update `FORK-CHANGES.md`** — add entry with rationale, files touched, and rebase risk. This file is the authoritative changelog.
3. Consider whether the patch is upstream-friendly (CAPsMAN wifi-package support, stable `configuration` label are good PR candidates).

## Known footguns

### `wifi_neighbor` collector is disabled — flat-snoop restarts the AP

Disabled in production as of 2026-04-19. Every `/interface/wifi flat-snoop` call on a CAPsMAN-bound `cap-wifiN` interface causes the physical AP to stop/start its SSID for the snoop duration, kicking all clients. Reproduces 1:1 with the 30s round-robin cadence. Not a code bug — it's CAPsMAN's behavior for flat-snoop on bound interfaces. See `homelab-infra/incidents/2026-04-19-0700-wifi-neighbor-ap-restart.md` for the full trace (use Loki with a time-bounded LogQL filter to verify; RouterOS log API fetch gets flooded out by wireless traffic).

### New collectors must register in TWO places

- `mktxp/flow/collector_registry.py` — dispatch (obvious)
- `mktxp/flow/router_entry.py` — add key to the `time_spent` init dict

If the `router_entry` key is missing, `KeyError` fires on every scrape after each collect. Metrics still flow (caught post-yield) but the log fills and duration accounting breaks.

### Test with `generate_latest()`, not just `mf.samples`

Iterating `mf.samples` in a local test misses Prometheus serialization errors. Non-string label values (floats, ints) crash only when the text encoder runs:

```python
from prometheus_client import CollectorRegistry
from prometheus_client.exposition import generate_latest
reg = CollectorRegistry()
reg.register(some_proxy_that_collects(your_collector))
generate_latest(reg)  # raises if any label is non-string
```

Real bug: `rate_mbps` was both a float gauge value and in the label set — local test passed, production crashed `/metrics` with `AttributeError: 'float' object has no attribute 'replace'`.

### `BaseDSProcessor.trimmed_records` drops fields not in `metric_labels`

Any record field NOT in `metric_labels` is stripped. If you later reference it as a gauge value, it silently defaults to `0`. Include every field the collector needs (labels AND gauge value keys) in the `metric_labels` arg to the datasource; pass a narrower list to each `gauge_collector`/`info_collector` for the Prometheus label shape.
