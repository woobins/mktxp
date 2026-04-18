# coding=utf8
## Copyright (c) 2020 Arseniy Kuznetsov
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


from mktxp.datasource.base_ds import BaseDSProcessor
from mktxp.datasource.wireless_ds import WirelessMetricsDataSource
from mktxp.flow.router_entry import RouterEntryWirelessType

class CapsmanInfo:
    @staticmethod
    def capsman_paths(router_entry):
        if router_entry.capsman_entry.wireless_type == RouterEntryWirelessType.DUAL:
            return ['/caps-man', f'/interface/wifi/capsman']
        elif router_entry.capsman_entry.wireless_type == RouterEntryWirelessType.WIRELESS:
            return ['/caps-man']
        else:    
            wireless_package = WirelessMetricsDataSource.wireless_package(router_entry.capsman_entry)
            return [f'/interface/{wireless_package}/capsman']

    @staticmethod
    def registration_table_paths(router_entry):
        if router_entry.capsman_entry.wireless_type == RouterEntryWirelessType.DUAL:
            return ['/caps-man/registration-table', f'/interface/wifi/registration-table']
        elif router_entry.capsman_entry.wireless_type == RouterEntryWirelessType.WIRELESS:
            return ['/caps-man/registration-table']
        else:    
            wireless_package = WirelessMetricsDataSource.wireless_package(router_entry.capsman_entry)
            return [f'/interface/{wireless_package}/registration-table']


class CapsmanCapsMetricsDataSource:
    ''' Caps Metrics data provider
    '''             
    @staticmethod
    def metric_records(router_entry, *, metric_labels = None):
        if metric_labels is None:
            metric_labels = []                
        try:
            remote_caps_records = []
            for capsman_path in CapsmanInfo.capsman_paths(router_entry.capsman_entry):
                remote_caps_records.extend(router_entry.capsman_entry.api_connection.router_api().get_resource(f'{capsman_path}/remote-cap').get())
            return BaseDSProcessor.trimmed_records(router_entry, router_records = remote_caps_records, metric_labels = metric_labels)
        except Exception as exc:
            print(f'Error getting CAPsMAN remote caps info from router {router_entry.capsman_entry.router_name}@{router_entry.capsman_entry.config_entry.hostname}: {exc}')
            return None

class CapsmanRegistrationsMetricsDataSource:
    ''' Capsman Registrations Metrics data provider
    '''             
    @staticmethod
    def metric_records(router_entry, *, metric_labels = None,  add_router_id = True):
        if metric_labels is None:
            metric_labels = []                
        try:
            registration_table_records = []
            for registration_table_path in CapsmanInfo.registration_table_paths(router_entry.capsman_entry):
                registration_table_records.extend(router_entry.capsman_entry.api_connection.router_api().get_resource(f'{registration_table_path}').get())
            
            # With wifiwave2, Mikrotik renamed the field 'rx-signal' to 'signal' 
            # For backward compatibility, including both variants
            for record in registration_table_records:
                if 'signal' in record:
                    record['rx-signal'] = record['signal']

            return BaseDSProcessor.trimmed_records(router_entry, router_records = registration_table_records, metric_labels = metric_labels, add_router_id = add_router_id)
        except Exception as exc:
            print(f'Error getting CAPsMAN registration table info from router {router_entry.capsman_entry.router_name}@{router_entry.capsman_entry.config_entry.hostname}: {exc}')
            return None


class CapsmanInterfacesDatasource:
    ''' Data provider for CAPsMaN interfaces.
        Supports legacy /caps-man/interface AND the new RouterOS 7 wifi package
        where CAPsMAN virtual interfaces live under /interface/wifi (named cap-wifi*).
    '''
    @staticmethod
    def metric_records(router_entry, *, metric_labels = None):
        if metric_labels is None:
            metric_labels = []
        wireless_type = router_entry.capsman_entry.wireless_type
        caps_interfaces = []
        try:
            # Legacy CAPsMAN package
            if wireless_type in (RouterEntryWirelessType.DUAL, RouterEntryWirelessType.WIRELESS):
                caps_interfaces.extend(
                    router_entry.capsman_entry.api_connection.router_api().get_resource('/caps-man/interface').get()
                )
            # New wifi/wifiwave2 CAPsMAN package — CAPsMAN virtual interfaces appear in
            # /interface/<pkg> with names prefixed "cap-wifi". The new API doesn't expose
            # current_state/current_channel/current_registered_clients under those field
            # names, but `configuration` IS present — which is a stable, user-meaningful
            # identifier that survives interface renumbering across router reboots.
            if wireless_type in (RouterEntryWirelessType.DUAL, RouterEntryWirelessType.WIFI, RouterEntryWirelessType.WIFIWAVE2):
                wireless_package = WirelessMetricsDataSource.wireless_package(router_entry.capsman_entry)
                all_wifi = router_entry.capsman_entry.api_connection.router_api().get_resource(f'/interface/{wireless_package}').get()
                caps_interfaces.extend([i for i in all_wifi if i.get('name', '').startswith('cap-wifi')])
            return BaseDSProcessor.trimmed_records(router_entry, router_records = caps_interfaces, metric_labels = metric_labels)
        except Exception as exc:
            print(f'Error getting CAPsMAN interfaces info from router {router_entry.capsman_entry.router_name}@{router_entry.capsman_entry.config_entry.hostname}: {exc}')
            return None

    @staticmethod
    def interface_to_configuration_map(router_entry):
        ''' Build a {interface_name: configuration_name} map for CAPsMAN virtual interfaces.
            Used to augment client registration records with a stable `configuration` label
            that does not shuffle when CAPsMAN renumbers cap-wifi interfaces on reboot.
        '''
        mapping = {}
        wireless_type = router_entry.capsman_entry.wireless_type
        try:
            if wireless_type in (RouterEntryWirelessType.DUAL, RouterEntryWirelessType.WIRELESS):
                for iface in router_entry.capsman_entry.api_connection.router_api().get_resource('/caps-man/interface').get():
                    mapping[iface.get('name', '')] = iface.get('configuration', '')
            if wireless_type in (RouterEntryWirelessType.DUAL, RouterEntryWirelessType.WIFI, RouterEntryWirelessType.WIFIWAVE2):
                wireless_package = WirelessMetricsDataSource.wireless_package(router_entry.capsman_entry)
                for iface in router_entry.capsman_entry.api_connection.router_api().get_resource(f'/interface/{wireless_package}').get():
                    name = iface.get('name', '')
                    if name.startswith('cap-wifi'):
                        mapping[name] = iface.get('configuration', '')
        except Exception:
            pass  # best-effort — absence is handled gracefully by callers
        return mapping
