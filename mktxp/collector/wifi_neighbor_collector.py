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


from mktxp.cli.config.config import MKTXPConfigKeys
from mktxp.collector.base_collector import BaseCollector
from mktxp.datasource.wifi_neighbor_ds import WifiNeighborDataSource


class WifiNeighborCollector(BaseCollector):
    """Neighbor AP metrics via `/interface/wifi/flat-snoop`.

    Runs only on CAPsMAN controllers (wifi-qcom/wifiwave2 package). Each scrape
    snoops ONE cap-wifiN radio (round-robin) to keep per-scrape cost bounded.
    Flat-snoop on the new wifi driver is non-disruptive to connected clients —
    the driver channel-hops opportunistically during the snoop window.
    """

    @staticmethod
    def collect(router_entry):
        if not getattr(router_entry.config_entry, MKTXPConfigKeys.FE_WIFI_NEIGHBOR_KEY, False):
            return
        if not router_entry.config_entry.capsman:
            # Neighbor snooping only makes sense where CAPsMAN is managing radios.
            return

        duration = getattr(router_entry.config_entry,
                           MKTXPConfigKeys.FE_WIFI_NEIGHBOR_DURATION_KEY, 7)
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            duration = 7

        # BaseDSProcessor.trimmed_records() STRIPS fields not in metric_labels —
        # so pass every field each metric-family needs (labels AND gauge value
        # keys). Separate sets for BSS vs STA since their label shape differs
        # (BSS carries ssid/rsn_akms/bss_on_time; STA carries mac_address).
        bss_keep = ['scanner_interface', 'configuration', 'address', 'ssid',
                    'frequency', 'rate_mbps', 'rsn_akms', 'pairwise_ciphers',
                    'signal', 'snr', 'noise', 'seen_frame_count',
                    'bss_on_time_seconds', 'last_seen_seconds']
        sta_keep = ['scanner_interface', 'configuration', 'mac_address',
                    'frequency', 'rate_mbps', 'signal', 'snr', 'noise',
                    'seen_frame_count', 'last_seen_seconds']
        result = WifiNeighborDataSource.metric_records(
            router_entry, bss_labels=bss_keep, sta_labels=sta_keep,
            duration=duration)
        bss_records = result.get('bss', []) if result else []
        sta_records = result.get('sta', []) if result else []

        # ----- Neighbor AP (BSS) metrics -----
        bss_labels = ['scanner_interface', 'configuration', 'address', 'ssid',
                      'frequency', 'rate_mbps', 'rsn_akms', 'pairwise_ciphers']
        if bss_records:
            for metric_name, doc, value_key in [
                ('wifi_neighbor_signal_dbm',
                 'Signal strength of neighbor AP as seen by scanner interface', 'signal'),
                ('wifi_neighbor_snr_db',
                 'Signal-to-noise ratio of neighbor AP in dB', 'snr'),
                ('wifi_neighbor_noise_dbm',
                 'Noise floor observed while snooping neighbor AP', 'noise'),
                ('wifi_neighbor_seen_frames',
                 'Number of frames observed from neighbor AP during snoop window',
                 'seen_frame_count'),
                ('wifi_neighbor_bss_on_time_seconds',
                 'How long the neighbor AP has been broadcasting (uptime proxy)',
                 'bss_on_time_seconds'),
                ('wifi_neighbor_last_seen_seconds',
                 'Time since last frame observed from neighbor AP',
                 'last_seen_seconds'),
                ('wifi_neighbor_rate_mbps',
                 'Negotiated data rate advertised by neighbor AP', 'rate_mbps'),
            ]:
                yield BaseCollector.gauge_collector(
                    metric_name, doc, bss_records, value_key, bss_labels)

            # Per-frequency neighbor count — channel congestion signal.
            counts = {}
            for r in bss_records:
                key = (r.get('scanner_interface', ''), r.get('frequency', ''))
                counts[key] = counts.get(key, 0) + 1
            count_records = [{
                MKTXPConfigKeys.ROUTERBOARD_NAME:
                    router_entry.router_id[MKTXPConfigKeys.ROUTERBOARD_NAME],
                MKTXPConfigKeys.ROUTERBOARD_ADDRESS:
                    router_entry.router_id[MKTXPConfigKeys.ROUTERBOARD_ADDRESS],
                'scanner_interface': k[0], 'frequency': k[1], 'count': v,
            } for k, v in counts.items()]
            yield BaseCollector.gauge_collector(
                'wifi_neighbor_count',
                'Number of neighbor APs observed per scanner interface per frequency',
                count_records, 'count', ['scanner_interface', 'frequency'])

            # Rogue flag — any BSSID NOT on the allowlist gets an info metric.
            # Allowlist is comma-separated MACs in config (our own radios +
            # anything else we consider "known"). If unset, skip the metric
            # entirely (no default of "everything is rogue").
            allowlist_raw = getattr(router_entry.config_entry,
                                    MKTXPConfigKeys.FE_WIFI_NEIGHBOR_ALLOWLIST_KEY, '')
            if allowlist_raw and str(allowlist_raw).lower() not in ('', 'none'):
                allowlist = {m.strip().upper() for m in
                             str(allowlist_raw).split(',') if m.strip()}
                rogue_records = [r for r in bss_records
                                 if r.get('address', '').upper() not in allowlist]
                if rogue_records:
                    yield BaseCollector.info_collector(
                        'wifi_neighbor_rogue',
                        'Neighbor APs not present in the configured allowlist',
                        rogue_records,
                        ['scanner_interface', 'address', 'ssid', 'frequency',
                         'rsn_akms', 'pairwise_ciphers'])

            yield BaseCollector.info_collector(
                'wifi_neighbor',
                'Neighbor AP details observed via flat-snoop',
                bss_records,
                ['scanner_interface', 'configuration', 'address', 'ssid',
                 'frequency', 'rate_mbps', 'rsn_akms', 'pairwise_ciphers'])

        # ----- Client (STA) metrics -----
        # Stations seen passively on this radio's channel — includes our own
        # clients PLUS neighbors' clients. Same-client visibility across
        # multiple scanners enables sticky-client detection and rough
        # localization. Dashboards filter to "our" clients via join against
        # mktxp_capsman_clients_* on mac_address.
        if sta_records:
            sta_labels = ['scanner_interface', 'configuration', 'mac_address',
                          'frequency', 'rate_mbps']
            for metric_name, doc, value_key in [
                ('wifi_client_signal_dbm',
                 'Signal strength of client as seen by scanner interface', 'signal'),
                ('wifi_client_snr_db',
                 'Signal-to-noise ratio of client signal in dB', 'snr'),
                ('wifi_client_noise_dbm',
                 'Noise floor observed while snooping client', 'noise'),
                ('wifi_client_seen_frames',
                 'Number of frames observed from client during snoop window',
                 'seen_frame_count'),
                ('wifi_client_last_seen_seconds',
                 'Time since last frame observed from client',
                 'last_seen_seconds'),
                ('wifi_client_rate_mbps',
                 'Most-recently observed client rate', 'rate_mbps'),
            ]:
                yield BaseCollector.gauge_collector(
                    metric_name, doc, sta_records, value_key, sta_labels)

            # Per-frequency client count.
            client_counts = {}
            for r in sta_records:
                key = (r.get('scanner_interface', ''), r.get('frequency', ''))
                client_counts[key] = client_counts.get(key, 0) + 1
            cc_records = [{
                MKTXPConfigKeys.ROUTERBOARD_NAME:
                    router_entry.router_id[MKTXPConfigKeys.ROUTERBOARD_NAME],
                MKTXPConfigKeys.ROUTERBOARD_ADDRESS:
                    router_entry.router_id[MKTXPConfigKeys.ROUTERBOARD_ADDRESS],
                'scanner_interface': k[0], 'frequency': k[1], 'count': v,
            } for k, v in client_counts.items()]
            yield BaseCollector.gauge_collector(
                'wifi_client_count',
                'Number of distinct clients observed per scanner per frequency',
                cc_records, 'count', ['scanner_interface', 'frequency'])
