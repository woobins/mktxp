# coding=utf8
## Copyright (c) 2026 Brett Elm
##
## This program is free software; you can redistribute it and/or
## modify it under the terms of the GNU General Public License
## as published by the Free Software Foundation; either version 2
## of the License, or (at your option) any later version.
##
## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.


import re
from mktxp.datasource.base_ds import BaseDSProcessor
from mktxp.datasource.capsman_ds import CapsmanInterfacesDatasource
from mktxp.datasource.wireless_ds import WirelessMetricsDataSource
from mktxp.flow.router_entry import RouterEntryWirelessType


_ROSDUR_RE = re.compile(r'(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?(?:(\d+)ms)?')


def _rosduration_to_seconds(value):
    """Convert RouterOS duration strings (e.g. '1d6h52m16s', '0ms', '12.3s') to float seconds.
    Returns 0.0 for unparseable input — flat-snoop emits '0ms' for just-seen BSSes and
    '1d6h52m16s' for long-lived ones; both need to collapse to a numeric gauge value.
    """
    if value is None or value == '':
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return 0.0
    m = _ROSDUR_RE.fullmatch(s)
    if not m:
        return 0.0
    w, d, h, mm, ss, ms = m.groups()
    total = 0.0
    total += int(w) * 7 * 86400 if w else 0
    total += int(d) * 86400 if d else 0
    total += int(h) * 3600 if h else 0
    total += int(mm) * 60 if mm else 0
    total += float(ss) if ss else 0
    total += int(ms) / 1000.0 if ms else 0
    return total


class WifiNeighborDataSource:
    """Data provider for wifi neighbor (flat-snoop) metrics.

    Calls `/interface/wifi/flat-snoop` once per CAPsMAN virtual interface on the
    controller. Each call blocks for `duration` seconds on the router while the
    driver channel-hops opportunistically (confirmed empirically non-disruptive
    to connected clients — see session notes).

    The API returns strictly more data than the CLI (signal, snr, noise,
    seen-frame-count, bss-on-time), so this datasource is the preferred path.
    """

    _ROUND_ROBIN_STATE = {}

    @staticmethod
    def metric_records(router_entry, *, bss_labels=None, sta_labels=None,
                       duration=7, round_robin=True):
        """Snoop one cap-wifiN radio (round-robin) and return both neighbor APs
        and passively-observed stations from the SAME snoop call.

        Returns: {'bss': [...], 'sta': [...]} — each list already passed through
        BaseDSProcessor.trimmed_records with the appropriate label set. Empty
        dict if the router isn't on the wifi/wifiwave2 package or the snoop
        fails entirely.

        filter-type='bsss,stas' returns both types from a single snoop — no
        extra scrape cost vs bss-only. `freq` rows are excluded (we derive
        frequency count from the BSS list directly).
        """
        if bss_labels is None:
            bss_labels = []
        if sta_labels is None:
            sta_labels = []

        wireless_type = router_entry.capsman_entry.wireless_type
        if wireless_type not in (RouterEntryWirelessType.DUAL,
                                 RouterEntryWirelessType.WIFI,
                                 RouterEntryWirelessType.WIFIWAVE2):
            # flat-snoop is only available on the new wifi / wifiwave2 package
            return {'bss': [], 'sta': []}

        try:
            api = router_entry.capsman_entry.api_connection.router_api()
            wireless_package = WirelessMetricsDataSource.wireless_package(router_entry.capsman_entry)
            all_wifi = api.get_resource(f'/interface/{wireless_package}').get()

            # Build .id + channel-info map for cap-wifiN interfaces only.
            iface_info = CapsmanInterfacesDatasource.interface_channel_info(router_entry)
            targets = []
            for iface in all_wifi:
                name = iface.get('name', '')
                if not name.startswith('cap-wifi'):
                    continue
                iface_id = iface.get('id')
                if not iface_id:
                    continue
                chinfo = iface_info.get(name, {})
                targets.append((name, iface_id, chinfo))

            # Round-robin: on each scrape, snoop ONE radio instead of all.
            # Each snoop blocks for `duration` seconds — snooping all 6 radios
            # per scrape would consume ~18s of scrape time, which risks timeout
            # and creates sustained latency windows on multiple radios
            # simultaneously. Round-robin spreads the load across scrapes.
            if round_robin and len(targets) > 1:
                router_key = router_entry.router_name
                prev_idx = WifiNeighborDataSource._ROUND_ROBIN_STATE.get(router_key, 0)
                idx = prev_idx % len(targets)
                WifiNeighborDataSource._ROUND_ROBIN_STATE[router_key] = idx + 1
                targets = [targets[idx]]

            bss_records, sta_records = [], []
            wifi_res = api.get_resource(f'/interface/{wireless_package}')
            for iface_name, iface_id, chinfo in targets:
                try:
                    rows = wifi_res.call('flat-snoop', {
                        '.id': iface_id,
                        'duration': f'{duration}s',
                        'filter-type': 'bsss,stas',
                    })
                except Exception as exc:
                    # Snoop can fail if the radio is mid-DFS-CAC or if another
                    # scan/snoop is in progress ("other tool running"). Skip
                    # this radio this scrape; next scrape will retry.
                    if hasattr(router_entry.config_entry, 'verbose_mode') \
                            and router_entry.config_entry.verbose_mode:
                        print(f'wifi_neighbor: snoop failed on {iface_name}: {exc}')
                    continue

                # Split by row type. flat-snoop returns one row per observation
                # (beacon for bss, probe/data frame for sta) — same entity
                # appears multiple times across scan rounds. Dedupe per
                # (address, frequency), keeping strongest-signal observation.
                # Aggregate seen-frame-count across observations for true
                # "how busy was this entity during the snoop window."
                best_bss, best_sta = {}, {}
                for row in rows:
                    d = dict(row)
                    rtype = d.get('type', '')
                    if rtype == 'bss':
                        bucket = best_bss
                    elif rtype == 'sta':
                        bucket = best_sta
                    else:
                        continue  # 'freq' rows or anything else — ignore
                    key = (d.get('address', ''), d.get('frequency', ''))
                    try:
                        sig = float(d.get('signal', '-999'))
                    except (ValueError, TypeError):
                        sig = -999.0
                    prev = bucket.get(key)
                    if prev is None:
                        bucket[key] = (sig, d)
                    elif sig > prev[0]:
                        # Carry accumulated frame count forward to new winner
                        d['_total_frames'] = prev[1].get('_total_frames', 0)
                        bucket[key] = (sig, d)
                    winner = bucket[key][1]
                    try:
                        winner['_total_frames'] = winner.get('_total_frames', 0) + \
                            int(d.get('seen-frame-count', 0) or 0)
                    except (ValueError, TypeError):
                        pass

                # Enrich + finalize each bucket into its record list
                def _finalize(bucket, out_list, *, is_bss):
                    for (_addr, _freq), (_sig, d) in bucket.items():
                        d['scanner_interface'] = iface_name
                        d['configuration'] = chinfo.get('configuration', '')
                        d['last_seen_seconds'] = _rosduration_to_seconds(d.get('last-seen', ''))
                        if is_bss:
                            d['bss_on_time_seconds'] = _rosduration_to_seconds(d.get('bss-on-time', ''))
                        else:
                            # STAs have MAC as 'address' — surface as mac_address
                            # so dashboards can join against capsman client metrics
                            # and AdGuard DHCP leases (both use mac_address).
                            d['mac_address'] = d.get('address', '')
                        # Numeric coercion — leave missing fields absent so the
                        # gauge defaults to 0 rather than polluting time series
                        # with spurious values.
                        for api_key, out_key in (('signal', 'signal'),
                                                 ('snr', 'snr'),
                                                 ('noise', 'noise'),
                                                 ('rate-mbps', 'rate_mbps'),
                                                 ('beacon-size', 'beacon_size')):
                            raw = d.get(api_key, '')
                            if raw == '' or raw is None:
                                continue
                            try:
                                d[out_key] = float(raw)
                            except (ValueError, TypeError):
                                pass
                        d['seen_frame_count'] = d.pop('_total_frames', 0)
                        out_list.append(d)

                _finalize(best_bss, bss_records, is_bss=True)
                _finalize(best_sta, sta_records, is_bss=False)

            return {
                'bss': BaseDSProcessor.trimmed_records(
                    router_entry, router_records=bss_records, metric_labels=bss_labels),
                'sta': BaseDSProcessor.trimmed_records(
                    router_entry, router_records=sta_records, metric_labels=sta_labels),
            }
        except Exception as exc:
            print(f'Error getting wifi neighbor (flat-snoop) info from router '
                  f'{router_entry.router_name}@{router_entry.config_entry.hostname}: {exc}')
            return {'bss': [], 'sta': []}
