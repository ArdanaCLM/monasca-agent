# Copyright (c) 2014 VMware, Inc.
# (C) Copyright 2015 Hewlett Packard Enterprise Development Company LP
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Implementation of Inspector abstraction for VMware vSphere"""

from oslo_config import cfg
from oslo_utils import units
from oslo_vmware import api

from monasca_agent.collector.virt import inspector as virt_inspector
from monasca_agent.collector.virt.vmware import vsphere_operations


opt_group = cfg.OptGroup(name='vmware',
                         title='Options for VMware')

OPTS = [
    cfg.HostAddressOpt('host_ip',
                       default='0.0.0.0',  # nosec
                       help='IP address of the VMware Vsphere host.'),
    cfg.IntOpt('host_port',
               default=443,
               help='Port of the VMware Vsphere host.'),
    cfg.StrOpt('host_username',
               default='',
               help='Username of VMware Vsphere.'),
    cfg.StrOpt('host_password',
               default='',
               help='Password of VMware Vsphere.',
               secret=True),
    cfg.IntOpt('api_retry_count',
               default=10,
               help='Number of times a VMware Vsphere API may be retried.'),
    cfg.FloatOpt('task_poll_interval',
                 default=0.5,
                 help='Sleep time in seconds for polling an ongoing async '
                      'task.'),
    cfg.StrOpt('wsdl_location',
               help='Optional vim service WSDL location '
                    'e.g http://<server>/vimService.wsdl. '
                    'Optional over-ride to default location for bug '
                    'work-arounds.'),
]

cfg.CONF.register_group(opt_group)
cfg.CONF.register_opts(OPTS, group=opt_group)

VC_AVERAGE_MEMORY_CONSUMED_CNTR = 'mem:consumed:average'
VC_AVERAGE_CPU_CONSUMED_CNTR = 'cpu:usage:average'
VC_NETWORK_RX_COUNTER = 'net:received:average'
VC_NETWORK_TX_COUNTER = 'net:transmitted:average'
VC_DISK_READ_RATE_CNTR = "disk:read:average"
VC_DISK_READ_REQUESTS_RATE_CNTR = "disk:numberReadAveraged:average"
VC_DISK_WRITE_RATE_CNTR = "disk:write:average"
VC_DISK_WRITE_REQUESTS_RATE_CNTR = "disk:numberWriteAveraged:average"


def get_api_session():
    api_session = api.VMwareAPISession(
        cfg.CONF.vmware.host_ip,
        cfg.CONF.vmware.host_username,
        cfg.CONF.vmware.host_password,
        cfg.CONF.vmware.api_retry_count,
        cfg.CONF.vmware.task_poll_interval,
        wsdl_loc=cfg.CONF.vmware.wsdl_location,
        port=cfg.CONF.vmware.host_port)
    return api_session


class VsphereInspector(virt_inspector.Inspector):

    def __init__(self):
        super(VsphereInspector, self).__init__()
        self._ops = vsphere_operations.VsphereOperations(
            get_api_session(), 1000)

    def inspect_cpu_util(self, instance, duration=None):
        vm_moid = self._ops.get_vm_moid(instance.id)
        if vm_moid is None:
            raise virt_inspector.InstanceNotFoundException(
                'VM %s not found in VMware Vsphere' % instance.id)
        cpu_util_counter_id = self._ops.get_perf_counter_id(
            VC_AVERAGE_CPU_CONSUMED_CNTR)
        cpu_util = self._ops.query_vm_aggregate_stats(
            vm_moid, cpu_util_counter_id, duration)

        # For this counter vSphere returns values scaled-up by 100, since the
        # corresponding API can't return decimals, but only longs.
        # For e.g. if the utilization is 12.34%, the value returned is 1234.
        # Hence, dividing by 100.
        cpu_util = cpu_util / 100
        return virt_inspector.CPUUtilStats(util=cpu_util)

    def inspect_vnic_rates(self, instance, duration=None):
        vm_moid = self._ops.get_vm_moid(instance.id)
        if not vm_moid:
            raise virt_inspector.InstanceNotFoundException(
                'VM %s not found in VMware Vsphere' % instance.id)

        vnic_stats = {}
        vnic_ids = set()

        for net_counter in (VC_NETWORK_RX_COUNTER, VC_NETWORK_TX_COUNTER):
            net_counter_id = self._ops.get_perf_counter_id(net_counter)
            vnic_id_to_stats_map = self._ops.query_vm_device_stats(
                vm_moid, net_counter_id, duration)
            vnic_stats[net_counter] = vnic_id_to_stats_map
            vnic_ids.update(vnic_id_to_stats_map.iterkeys())

        # Stats provided from vSphere are in KB/s, converting it to B/s.
        for vnic_id in vnic_ids:
            rx_bytes_rate = (vnic_stats[VC_NETWORK_RX_COUNTER]
                             .get(vnic_id, 0) * units.Ki)
            tx_bytes_rate = (vnic_stats[VC_NETWORK_TX_COUNTER]
                             .get(vnic_id, 0) * units.Ki)

            stats = virt_inspector.InterfaceRateStats(rx_bytes_rate,
                                                      tx_bytes_rate)
            interface = virt_inspector.Interface(
                name=vnic_id,
                mac=None,
                fref=None,
                parameters=None)
            yield (interface, stats)

    def inspect_memory_usage(self, instance, duration=None):
        vm_moid = self._ops.get_vm_moid(instance.id)
        if vm_moid is None:
            raise virt_inspector.InstanceNotFoundException(
                'VM %s not found in VMware Vsphere' % instance.id)
        mem_counter_id = self._ops.get_perf_counter_id(
            VC_AVERAGE_MEMORY_CONSUMED_CNTR)
        memory = self._ops.query_vm_aggregate_stats(
            vm_moid, mem_counter_id, duration)
        # Stat provided from vSphere is in KB, converting it to MB.
        memory = memory / units.Ki
        return virt_inspector.MemoryUsageStats(usage=memory)

    def inspect_disk_rates(self, instance, duration=None):
        vm_moid = self._ops.get_vm_moid(instance.id)
        if not vm_moid:
            raise virt_inspector.InstanceNotFoundException(
                'VM %s not found in VMware Vsphere' % instance.id)

        disk_stats = {}
        disk_ids = set()
        disk_counters = [
            VC_DISK_READ_RATE_CNTR,
            VC_DISK_READ_REQUESTS_RATE_CNTR,
            VC_DISK_WRITE_RATE_CNTR,
            VC_DISK_WRITE_REQUESTS_RATE_CNTR
        ]

        for disk_counter in disk_counters:
            disk_counter_id = self._ops.get_perf_counter_id(disk_counter)
            disk_id_to_stat_map = self._ops.query_vm_device_stats(
                vm_moid, disk_counter_id, duration)
            disk_stats[disk_counter] = disk_id_to_stat_map
            disk_ids.update(disk_id_to_stat_map.iterkeys())

        for disk_id in disk_ids:

            def stat_val(counter_name):
                return disk_stats[counter_name].get(disk_id, 0)

            disk = virt_inspector.Disk(device=disk_id)
            # Stats provided from vSphere are in KB/s, converting it to B/s.
            disk_rate_info = virt_inspector.DiskRateStats(
                read_bytes_rate=stat_val(VC_DISK_READ_RATE_CNTR) * units.Ki,
                read_requests_rate=stat_val(VC_DISK_READ_REQUESTS_RATE_CNTR),
                write_bytes_rate=stat_val(VC_DISK_WRITE_RATE_CNTR) * units.Ki,
                write_requests_rate=stat_val(VC_DISK_WRITE_REQUESTS_RATE_CNTR)
            )
            yield(disk, disk_rate_info)
