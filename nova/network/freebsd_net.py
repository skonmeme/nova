# Copyright (c) 2011 X.commerce, a business unit of eBay Inc.
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
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

"""Implements vlans, bridges, and iptables rules using linux utilities."""

import calendar
import inspect
import os
import re
import time
import json

import netaddr
import netifaces
import socket
import struct

from oslo_concurrency import processutils
from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_utils import excutils
from oslo_utils import fileutils
from oslo_utils import importutils
from oslo_utils import timeutils
import six

import nova.conf
from nova import exception
from nova.i18n import _, _LE, _LW
from nova.network import model as network_model
from nova import objects
from nova.pci import utils as pci_utils
from nova import utils

LOG = logging.getLogger(__name__)


CONF = nova.conf.CONF


# NOTE(vish): Iptables supports chain names of up to 28 characters,  and we
#             add up to 12 characters to binary_name which is used as a prefix,
#             so we limit it to 16 characters.
#             (max_chain_name_length - len('-POSTROUTING') == 16)
def get_binary_name():
    """Grab the name of the binary we're running in."""
    return os.path.basename(inspect.stack()[-1][1])[:16]

binary_name = get_binary_name()


# NOTE(jkoelker) This is just a nice little stub point since mocking
#                builtins with mox is a nightmare
def write_to_file(file, data, mode='w'):
    with open(file, mode) as f:
        f.write(data)


def is_pid_cmdline_correct(pid, match):
    """Ensure that the cmdline for a pid seems sane

    Because pids are recycled, blindly killing by pid is something to
    avoid. This provides the ability to include a substring that is
    expected in the cmdline as a safety check.
    """
    try:
        with open('/proc/%d/cmdline' % pid) as f:
            cmdline = f.read()
            return match in cmdline
    except EnvironmentError:
        return False


def metadata_forward():
    """Create forwarding rule for metadata."""
    firewall_manager.add_rule("rdr proto tcp from any to 169.254.169.254 "
                              "port 80 -> %s port %s" %
                              (CONF.metadata_host, CONF.metadata_port))
    firewall_manager.add_rule("pass out route-to (lo0 127.0.0.1) proto tcp "
                              "from any to 169.254.169.254 port 80")
    firewall_manager.apply()


def metadata_accept():
    """Create the filter accept rule for metadata."""
    firewall_manager.add_rule("pass in inet proto tcp from any to "
                              "169.254.169.254 port = http "
                              "flags S/SA keep state")
    firewall_manager.apply()


def init_host(ip_range, is_external=False):
    """Basic networking setup goes here."""
    # NOTE(devcamcar): Cloud public SNAT entries and the default
    # SNAT rule for outbound traffic.

    firewall_manager.add_snat_rule(ip_range, is_external)
    if is_external:
        for snat_range in CONF.force_snat_range:
            firewall_manager.add_rule("pass quick inet from %s to %s" %
                                      (ip_range, snat_range))
    firewall_manager.add_rule("pass quick inet from %s to %s/32" %
                              (ip_range, CONF.metadata_host))
    for dmz in CONF.dmz_cidr:
        firewall_manager.add_rule("pass quick inet from %s to %s" %
                                  (ip_range, dmz))

    """
    iptables_manager.ipv4['nat'].add_rule('POSTROUTING',
                                          '-s %(range)s -d %(range)s '
                                          '-m conntrack ! --ctstate DNAT '
                                          '-j ACCEPT' %
                                          {'range': ip_range})
    """
    firewall_manager.apply()


def send_arp_for_ip(ip, device, count):
    out, err = _execute('arping', '-U', '-i', device, '-c', str(count), ip,
                        run_as_root=True, check_exit_code=False)

    if err:
        LOG.debug('arping error for IP %s', ip)


def bind_floating_ip(floating_ip, device):
    """Bind IP to public interface."""
    _execute('ifconfig', device, str(floating_ip) + '/32', 'add',
             run_as_root=True, check_exit_code=0)

    if CONF.send_arp_for_ha and CONF.send_arp_for_ha_count > 0:
        send_arp_for_ip(floating_ip, device, CONF.send_arp_for_ha_count)


def unbind_floating_ip(floating_ip, device):
    """Unbind a public IP from public interface."""
    _execute('ifconfig', device, str(floating_ip) + '/32', 'delete',
             run_as_root=True, check_exit_code=0)


def ensure_metadata_ip():
    """Sets up local metadata IP."""
    _execute('ifconfig', 'lo0', 'alias', '169.254.169.254/32',
             run_as_root=True, check_exit_code=0)


def ensure_vpn_forward(public_ip, port, private_ip):
    """Sets up forwarding rules for vlan."""
    firewall_manager.add_rule("pass in proto udp "
                              "from any to %s port 1194 " %
                              (private_ip))
    firewall_manager.add_rule("rdr proto udp from any to %s port %s -> "
                              "%s port 1194" %
                              (public_ip, port, private_ip))
    firewall_manager.apply()


def ensure_floating_forward(floating_ip, fixed_ip, device, network):
    """Ensure floating IP forwarding rule."""
    firewall_manager.ensure_floating_rules(floating_ip, fixed_ip, device)
    if device != network['bridge']:
        firewall_manager.ensure_in_network_traffic_rules(fixed_ip, network)
    firewall_manager.apply()


def remove_floating_forward(floating_ip, fixed_ip, device, network):
    """Remove forwarding for floating IP."""
    firewall_manager.remove_floating_rules(floating_ip, fixed_ip, device)
    if device != network['bridge']:
        firewall_manager.remove_in_network_traffic_rules(fixed_ip, network)
    firewall_manager.apply()


def clean_conntrack(fixed_ip):
    pass


def _enable_ipv4_forwarding():
    sysctl_key = 'net.inet.ip.forwarding'
    stdout, stderr = _execute('sysctl', '-n', sysctl_key)
    if stdout.strip() is not '1':
        _execute('sysctl', '%s=1' % sysctl_key, run_as_root=True)


@utils.synchronized('lock_gateway', external=True)
def initialize_gateway_device(dev, network_ref):
    if not network_ref:
        return

    _enable_ipv4_forwarding()

    # NOTE(vish): The ip for dnsmasq has to be the first address on the
    #             bridge for it to respond to requests properly
    try:
        prefix = network_ref.cidr.prefixlen
    except AttributeError:
        prefix = network_ref['cidr'].rpartition('/')[2]

    full_ip = '%s/%s' % (network_ref['dhcp_server'], prefix)
    new_ip_params = [['inet', full_ip, 'broadcast', network_ref['broadcast']]]
    old_ip_params = []
    out, err = _execute('ifconfig', dev)
    for line in out.split('\n'):
        fields = line.split()
        if fields and fields[0] == 'inet':
            old_ip_params.append(fields)
            if _address_to_cidr(fields[1], fields[3]) != full_ip:
                new_ip_params.append(fields)
    if not old_ip_params or _address_to_cidr(old_ip_params[0][1], old_ip_params[0][3]) != full_ip:
        old_routes = []
        result = _execute('netstat', '-nrW', '-f', 'inet')
        if result:
            out, err = result
            for line in out.split('\n'):
                fields = line.split()
                if len(fields) > 6 and (fields[6] == dev) and ('G' in fields[2]):
                    old_routes.append(fields)
                    _execute('route', '-q', 'delete', fields[0], fields[1],
                             run_as_root=True)
        for ip_params in old_ip_params:
            _execute(*_ifconfig_tail_cmd(dev, ip_params, 'delete'),
                     run_as_root=True)
        for ip_params in new_ip_params:
            _execute(*_ifconfig_tail_cmd(dev, ip_params, 'add'),
                     run_as_root=True)

        for fields in old_routes:
            _execute('route', '-q', 'add', fields[0], fields[1],
                     run_as_root=True)
        if CONF.send_arp_for_ha and CONF.send_arp_for_ha_count > 0:
            send_arp_for_ip(network_ref['dhcp_server'], dev,
                            CONF.send_arp_for_ha_count)
    if CONF.use_ipv6:
        _execute('ifconfig', dev, 'inet6', network_ref['cidr_v6'],
                 run_as_root=True)


def get_dhcp_leases(context, network_ref):
    """Return a network's hosts config in dnsmasq leasefile format."""
    hosts = []
    host = None
    if network_ref['multi_host']:
        host = CONF.host
    for fixedip in objects.FixedIPList.get_by_network(context,
                                                      network_ref,
                                                      host=host):
        # NOTE(cfb): Don't return a lease entry if the IP isn't
        #            already leased
        if fixedip.leased:
            hosts.append(_host_lease(fixedip))

    return '\n'.join(hosts)


def get_dhcp_hosts(context, network_ref, fixedips):
    """Get network's hosts config in dhcp-host format."""
    hosts = []
    macs = set()
    for fixedip in fixedips:
        if fixedip.allocated:
            if fixedip.virtual_interface.address not in macs:
                hosts.append(_host_dhcp(fixedip))
                macs.add(fixedip.virtual_interface.address)
    return '\n'.join(hosts)


def get_dns_hosts(context, network_ref):
    """Get network's DNS hosts in hosts format."""
    hosts = []
    for fixedip in objects.FixedIPList.get_by_network(context, network_ref):
        if fixedip.allocated:
            hosts.append(_host_dns(fixedip))
    return '\n'.join(hosts)


def _add_dnsmasq_accept_rules(dev):
    """Allow DHCP and DNS traffic through to dnsmasq."""
    for port in [67, 53]:
        for proto in ['udp', 'tcp']:
            firewall_manager.add_rule("pass in on %s inet proto %s "
                                      "from any to any port %s" %
                                      (dev, proto, port))
    firewall_manager.apply()


def _remove_dnsmasq_accept_rules(dev):
    """Remove DHCP and DNS traffic allowed through to dnsmasq."""
    for port in [67, 53]:
        for proto in ['udp', 'tcp']:
            firewall_manager.remove_rule("pass in on %s inet proto %s "
                                         "from any to any port %s" %
                                         (dev, proto, port))
    firewall_manager.apply()


def get_dhcp_opts(context, network_ref, fixedips):
    """Get network's hosts config in dhcp-opts format."""
    gateway = network_ref['gateway']
    # NOTE(vish): if we are in multi-host mode and we are not sharing
    #             addresses, then we actually need to hand out the
    #             dhcp server address as the gateway.
    if network_ref['multi_host'] and not (network_ref['share_address'] or
                                          CONF.share_dhcp_address):
        gateway = network_ref['dhcp_server']
    hosts = []
    if CONF.use_single_default_gateway:
        for fixedip in fixedips:
            if fixedip.allocated:
                vif_id = fixedip.virtual_interface_id
                if fixedip.default_route:
                    hosts.append(_host_dhcp_opts(vif_id, gateway))
                else:
                    hosts.append(_host_dhcp_opts(vif_id))
    else:
        hosts.append(_host_dhcp_opts(None, gateway))
    return '\n'.join(hosts)


def release_dhcp(dev, address, mac_address):
    if device_exists(dev):
        try:
            utils.execute('dhcp_release', dev, address, mac_address,
                          run_as_root=True)
        except processutils.ProcessExecutionError:
            raise exception.NetworkDhcpReleaseFailed(address=address,
                                                     mac_address=mac_address)


def update_dhcp(context, dev, network_ref):
    conffile = _dhcp_file(dev, 'conf')
    host = None
    if network_ref['multi_host']:
        host = CONF.host
    fixedips = objects.FixedIPList.get_by_network(context,
                                                  network_ref,
                                                  host=host)
    write_to_file(conffile, get_dhcp_hosts(context, network_ref, fixedips))
    restart_dhcp(context, dev, network_ref, fixedips)


def update_dns(context, dev, network_ref):
    hostsfile = _dhcp_file(dev, 'hosts')
    host = None
    if network_ref['multi_host']:
        host = CONF.host
    fixedips = objects.FixedIPList.get_by_network(context,
                                                  network_ref,
                                                  host=host)
    write_to_file(hostsfile, get_dns_hosts(context, network_ref))
    restart_dhcp(context, dev, network_ref, fixedips)


def kill_dhcp(dev):
    pid = _dnsmasq_pid_for(dev)
    if pid:
        # Check that the process exists and looks like a dnsmasq process
        conffile = _dhcp_file(dev, 'conf')
        if is_pid_cmdline_correct(pid, conffile.split('/')[-1]):
            _execute('kill', '-9', pid, run_as_root=True)
        else:
            LOG.debug('Pid %d is stale, skip killing dnsmasq', pid)
    _remove_dnsmasq_accept_rules(dev)


# NOTE(ja): Sending a HUP only reloads the hostfile, so any
#           configuration options (like dchp-range, vlan, ...)
#           aren't reloaded.
@utils.synchronized('dnsmasq_start')
def restart_dhcp(context, dev, network_ref, fixedips):
    """(Re)starts a dnsmasq server for a given network.

    If a dnsmasq instance is already running then send a HUP
    signal causing it to reload, otherwise spawn a new instance.

    """
    conffile = _dhcp_file(dev, 'conf')

    optsfile = _dhcp_file(dev, 'opts')
    write_to_file(optsfile, get_dhcp_opts(context, network_ref, fixedips))
    os.chmod(optsfile, 0o644)

    # Make sure dnsmasq can actually read it (it setuid()s to "nobody")
    os.chmod(conffile, 0o644)

    pid = _dnsmasq_pid_for(dev)

    # if dnsmasq is already running, then tell it to reload
    if pid:
        if is_pid_cmdline_correct(pid, conffile.split('/')[-1]):
            try:
                _execute('kill', '-HUP', pid, run_as_root=True)
                _add_dnsmasq_accept_rules(dev)
                return
            except Exception as exc:
                LOG.error(_LE('kill -HUP dnsmasq threw %s'), exc)
        else:
            LOG.debug('Pid %d is stale, relaunching dnsmasq', pid)

    cmd = ['env',
           'CONFIG_FILE=%s' % jsonutils.dumps(CONF.dhcpbridge_flagfile),
           'NETWORK_ID=%s' % str(network_ref['id']),
           'dnsmasq',
           '--strict-order',
           '--bind-interfaces',
           '--conf-file=%s' % CONF.dnsmasq_config_file,
           '--pid-file=%s' % _dhcp_file(dev, 'pid'),
           '--dhcp-optsfile=%s' % _dhcp_file(dev, 'opts'),
           '--listen-address=%s' % network_ref['dhcp_server'],
           '--except-interface=lo',
           '--dhcp-range=set:%s,%s,static,%s,%ss' %
                         (network_ref['label'],
                          network_ref['dhcp_start'],
                          network_ref['netmask'],
                          CONF.dhcp_lease_time),
           '--dhcp-lease-max=%s' % len(netaddr.IPNetwork(network_ref['cidr'])),
           '--dhcp-hostsfile=%s' % _dhcp_file(dev, 'conf'),
           '--dhcp-script=%s' % CONF.dhcpbridge,
           '--no-hosts',
           '--leasefile-ro']

    # dnsmasq currently gives an error for an empty domain,
    # rather than ignoring.  So only specify it if defined.
    if CONF.dhcp_domain:
        cmd.append('--domain=%s' % CONF.dhcp_domain)

    dns_servers = CONF.dns_server
    if CONF.use_network_dns_servers:
        if network_ref.get('dns1'):
            dns_servers.append(network_ref.get('dns1'))
        if network_ref.get('dns2'):
            dns_servers.append(network_ref.get('dns2'))
    if network_ref['multi_host']:
        cmd.append('--addn-hosts=%s' % _dhcp_file(dev, 'hosts'))
    if dns_servers:
        cmd.append('--no-resolv')
    for dns_server in dns_servers:
        cmd.append('--server=%s' % dns_server)

    _execute(*cmd, run_as_root=True)

    _add_dnsmasq_accept_rules(dev)


@utils.synchronized('radvd_start')
def update_ra(context, dev, network_ref):
    conffile = _ra_file(dev, 'conf')
    conf_str = """
interface %s
{
   AdvSendAdvert on;
   MinRtrAdvInterval 3;
   MaxRtrAdvInterval 10;
   prefix %s
   {
        AdvOnLink on;
        AdvAutonomous on;
   };
};
""" % (dev, network_ref['cidr_v6'])
    write_to_file(conffile, conf_str)

    # Make sure radvd can actually read it (it setuid()s to "nobody")
    os.chmod(conffile, 0o644)

    pid = _ra_pid_for(dev)

    # if radvd is already running, then tell it to reload
    if pid:
        if is_pid_cmdline_correct(pid, conffile):
            try:
                _execute('kill', pid, run_as_root=True)
            except Exception as exc:
                LOG.error(_LE('killing radvd threw %s'), exc)
        else:
            LOG.debug('Pid %d is stale, relaunching radvd', pid)

    cmd = ['radvd',
           '-C', '%s' % _ra_file(dev, 'conf'),
           '-p', '%s' % _ra_file(dev, 'pid')]

    _execute(*cmd, run_as_root=True)


def _host_lease(fixedip):
    """Return a host string for an address in leasefile format."""
    timestamp = timeutils.utcnow()
    seconds_since_epoch = calendar.timegm(timestamp.utctimetuple())
    return '%d %s %s %s *' % (seconds_since_epoch + CONF.dhcp_lease_time,
                              fixedip.virtual_interface.address,
                              fixedip.address,
                              fixedip.instance.hostname or '*')


def _host_dhcp_network(vif_id):
    return 'NW-%s' % vif_id


def _host_dhcp(fixedip):
    """Return a host string for an address in dhcp-host format."""
    # NOTE(cfb): dnsmasq on linux only supports 64 characters in the hostname
    #            field (LP #1238910). Since the . counts as a character we need
    #            to truncate the hostname to only 63 characters.
    hostname = fixedip.instance.hostname
    if len(hostname) > 63:
        LOG.warning(_LW('hostname %s too long, truncating.'), hostname)
        hostname = fixedip.instance.hostname[:2] + '-' +\
                   fixedip.instance.hostname[-60:]
    if CONF.use_single_default_gateway:
        net = _host_dhcp_network(fixedip.virtual_interface_id)
        return '%s,%s.%s,%s,net:%s' % (fixedip.virtual_interface.address,
                               hostname,
                               CONF.dhcp_domain,
                               fixedip.address,
                               net)
    else:
        return '%s,%s.%s,%s' % (fixedip.virtual_interface.address,
                               hostname,
                               CONF.dhcp_domain,
                               fixedip.address)


def _host_dns(fixedip):
    return '%s\t%s.%s' % (fixedip.address,
                          fixedip.instance.hostname,
                          CONF.dhcp_domain)


def _host_dhcp_opts(vif_id=None, gateway=None):
    """Return an empty gateway option."""
    values = []
    if vif_id is not None:
        values.append(_host_dhcp_network(vif_id))
    # NOTE(vish): 3 is the dhcp option for gateway.
    values.append('3')
    if gateway:
        values.append('%s' % gateway)
    return ','.join(values)


def _execute(*cmd, **kwargs):
    """Wrapper around utils._execute for fake_network."""
    if CONF.fake_network:
        LOG.debug('FAKE NET: %s', ' '.join(map(str, cmd)))
        return 'fake', 0
    else:
        return utils.execute(*cmd, **kwargs)


def device_exists(device):
    """Check if ethernet device exists."""
    try:
        _execute('ifconfig', device, run_as_root=True, check_exit_code=[0])
    except processutils.ProcessExecutionError:
        return False
    else:
        return True


def _dhcp_file(dev, kind):
    """Return path to a pid, leases, hosts or conf file for a bridge/device."""
    fileutils.ensure_tree(CONF.networks_path)
    return os.path.abspath('%s/nova-%s.%s' % (CONF.networks_path,
                                              dev,
                                              kind))


def _ra_file(dev, kind):
    """Return path to a pid or conf file for a bridge/device."""
    fileutils.ensure_tree(CONF.networks_path)
    return os.path.abspath('%s/nova-ra-%s.%s' % (CONF.networks_path,
                                              dev,
                                              kind))


def _dnsmasq_pid_for(dev):
    """Returns the pid for prior dnsmasq instance for a bridge/device.

    Returns None if no pid file exists.

    If machine has rebooted pid might be incorrect (caller should check).

    """
    pid_file = _dhcp_file(dev, 'pid')

    if os.path.exists(pid_file):
        try:
            with open(pid_file, 'r') as f:
                return int(f.read())
        except (ValueError, IOError):
            return None


def _ra_pid_for(dev):
    """Returns the pid for prior radvd instance for a bridge/device.

    Returns None if no pid file exists.

    If machine has rebooted pid might be incorrect (caller should check).

    """
    pid_file = _ra_file(dev, 'pid')

    if os.path.exists(pid_file):
        with open(pid_file, 'r') as f:
            return int(f.read())


def _address_to_cidr(address, hexmask):
    """Produce a CIDR format address/netmask."""
    netmask = socket.inet_ntoa(struct.pack(">I", int(hexmask, 16)))
    ip_cidr = netaddr.IPNetwork("%s/%s" % (address, netmask))
    return str(ip_cidr)


def _ifconfig_tail_cmd(netif, params, action):
    """Construct ifconfig command"""
    cmd = ['ifconfig', netif]
    cmd.extend(params)
    cmd.extend([action])
    return cmd


def _set_device_mtu(dev, mtu=None):
    """Set the device MTU."""
    if mtu:
        utils.execute('ifconfig', dev, 'mtu', mtu,
                      run_as_root=True, check_exit_code=0)


def _ovs_vsctl(args):
    full_args = ['ovs-vsctl', '--timeout=%s' % CONF.ovs_vsctl_timeout] + args
    try:
        return utils.execute(*full_args, run_as_root=True)
    except Exception as e:
        LOG.error(_LE("Unable to execute %(cmd)s. Exception: %(exception)s"),
                  {'cmd': full_args, 'exception': e})
        raise exception.OvsConfigurationFailure(inner_exception=e)


def _create_ovs_vif_cmd(bridge, dev, iface_id, mac,
                        instance_id, interface_type=None):
    cmd = ['--', '--if-exists', 'del-port', dev, '--',
            'add-port', bridge, dev,
            '--', 'set', 'Interface', dev,
            'external-ids:iface-id=%s' % iface_id,
            'external-ids:iface-status=active',
            'external-ids:attached-mac=%s' % mac,
            'external-ids:vm-uuid=%s' % instance_id]
    if interface_type:
        cmd += ['type=%s' % interface_type]
    return cmd


def create_ovs_vif_port(bridge, dev, iface_id, mac, instance_id,
                        mtu=None, interface_type=None):
    _ovs_vsctl(_create_ovs_vif_cmd(bridge, dev, iface_id,
                                   mac, instance_id,
                                   interface_type))
    # Note at present there is no support for setting the
    # mtu for vhost-user type ports.
    if interface_type != network_model.OVS_VHOSTUSER_INTERFACE_TYPE:
        _set_device_mtu(dev, mtu)
    else:
        LOG.debug("MTU not set on %(interface_name)s interface "
                  "of type %(interface_type)s.",
                  {'interface_name': dev,
                   'interface_type': interface_type})


def delete_ovs_vif_port(bridge, dev, delete_dev=True):
    _ovs_vsctl(['--', '--if-exists', 'del-port', bridge, dev])
    if delete_dev:
        delete_net_dev(dev)


def create_tap_dev(dev, mac_address=None):
    if not device_exists(dev):
        utils.execute('ifconfig', 'tap', 'create', 'name', dev,
                      run_as_root=True, check_exit_code=[0])
        if mac_address:
            utils.execute('ifconfig', dev, 'ether', mac_address,
                          run_as_root=True, check_exit_code=[0])
        utils.execute('ifconfig', dev, 'up',
                      run_as_root=True, check_exit_code=[0])


def delete_net_dev(dev):
    """Delete a network device only if it exists."""
    if device_exists(dev):
        try:
            utils.execute('ifconfig', dev, 'destroy',
                          run_as_root=True, check_exit_code=0)
            LOG.debug("Net device removed: '%s'", dev)
        except processutils.ProcessExecutionError:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE("Failed removing net device: '%s'"), dev)


def delete_bridge_dev(dev):
    """Delete a network bridge."""
    if device_exists(dev):
        try:
            utils.execute('ifconfig', dev, 'down', run_as_root=True)
            utils.execute('ifconfig', dev, 'destroy', run_as_root=True)
        except processutils.ProcessExecutionError:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE("Failed removing bridge device: '%s'"), dev)


# Similar to compute virt layers, the FreeBSD network node
# code uses a flexible driver model to support different ways
# of creating ethernet interfaces and attaching them to the network.
# In the case of a network host, these interfaces
# act as gateway/dhcp/vpn/etc. endpoints not VM interfaces.
interface_driver = None


def _get_interface_driver():
    global interface_driver
    if not interface_driver:
        interface_driver = importutils.import_object(
                CONF.freebsdnet_interface_driver)
    return interface_driver


def plug(network, mac_address, gateway=True):
    return _get_interface_driver().plug(network, mac_address, gateway)


def unplug(network):
    return _get_interface_driver().unplug(network)


def get_dev(network):
    return _get_interface_driver().get_dev(network)


class FreeBSDNetInterfaceDriver(object):
    """Abstract class that defines generic network host API
    for all FreeBSD interface drivers.
    """

    def plug(self, network, mac_address):
        """Create FreeBSD device, return device name."""
        raise NotImplementedError()

    def unplug(self, network):
        """Destroy FreeBSD device, return device name."""
        raise NotImplementedError()

    def get_dev(self, network):
        """Get device name."""
        raise NotImplementedError()


# plugs interfaces using FreeBSD Bridge
class FreeBSDBridgeInterfaceDriver(FreeBSDNetInterfaceDriver):

    def plug(self, network, mac_address, gateway=True):
        vlan = network.get('vlan')
        if vlan is not None:
            iface = CONF.vlan_interface or network['bridge_interface']
            FreeBSDBridgeInterfaceDriver.ensure_vlan_bridge(
                           vlan,
                           network['bridge'],
                           iface,
                           network,
                           mac_address,
                           network.get('mtu'))
            iface = 'vlan%s' % vlan
        else:
            iface = CONF.flat_interface or network['bridge_interface']
            FreeBSDBridgeInterfaceDriver.ensure_bridge(
                          network['bridge'],
                          iface,
                          network, gateway)

        if network['share_address'] or CONF.share_dhcp_address:
            isolate_dhcp_address(iface, network['dhcp_server'])
        # NOTE(vish): applying here so we don't get a lock conflict
        firewall_manager.apply()
        return network['bridge']

    def unplug(self, network, gateway=True):
        vlan = network.get('vlan')
        if vlan is not None:
            iface = 'vlan%s' % vlan
            FreeBSDBridgeInterfaceDriver.remove_vlan_bridge(vlan,
                                                          network['bridge'])
        else:
            iface = CONF.flat_interface or network['bridge_interface']
            FreeBSDBridgeInterfaceDriver.remove_bridge(network['bridge'],
                                                     gateway)

        if network['share_address'] or CONF.share_dhcp_address:
            remove_isolate_dhcp_address(iface, network['dhcp_server'])

        firewall_manager.apply()
        return self.get_dev(network)

    def get_dev(self, network):
        return network['bridge']

    @staticmethod
    def ensure_vlan_bridge(vlan_num, bridge, bridge_interface,
                           net_attrs=None, mac_address=None,
                           mtu=None):
        """Create a vlan and bridge unless they already exist."""
        interface = FreeBSDBridgeInterfaceDriver.ensure_vlan(vlan_num,
                                               bridge_interface, mac_address,
                                               mtu)
        FreeBSDBridgeInterfaceDriver.ensure_bridge(bridge, interface, net_attrs)
        return interface

    @staticmethod
    def remove_vlan_bridge(vlan_num, bridge):
        """Delete a bridge and vlan."""
        FreeBSDBridgeInterfaceDriver.remove_bridge(bridge)
        FreeBSDBridgeInterfaceDriver.remove_vlan(vlan_num)

    @staticmethod
    @utils.synchronized('lock_vlan', external=True)
    def ensure_vlan(vlan_num, bridge_interface, mac_address=None, mtu=None,
                    interface=None):
        """Create a vlan unless it already exists."""
        if interface is None:
            interface = 'vlan%s' % vlan_num
        if not device_exists(interface):
            LOG.debug('Starting VLAN interface %s', interface)
            out, err = _execute('ifconfig', 'vlan', 'create',
                     'vlan', vlan_num,
                     'vlandev', bridge_interface,
                     'name', interface,
                     run_as_root=True)
            if err and 'File exists' not in err:
                msg = _('Failed to add vlan: %s') % err
                raise exception.NovaException(msg)
            # (danwent) the bridge will inherit this address, so we want to
            # make sure it is the value set from the NetworkManager
            if mac_address:
                _execute('ifconfig', interface, 'ether', mac_address,
                         run_as_root=True)
            _execute('ifconfig',interface, 'up',
                     run_as_root=True)
        # NOTE(vish): set mtu every time to ensure that changes to mtu get
        #             propagated
        _set_device_mtu(interface, mtu)
        return interface

    @staticmethod
    @utils.synchronized('lock_vlan', external=True)
    def remove_vlan(vlan_num):
        """Delete a vlan."""
        vlan_interface = 'vlan%s' % vlan_num
        delete_net_dev(vlan_interface)

    @staticmethod
    @utils.synchronized('lock_bridge', external=True)
    def ensure_bridge(bridge, interface, net_attrs=None, gateway=True,
                      filtering=True):
        """Create a bridge unless it already exists.

        :param interface: the interface to create the bridge on.
        :param net_attrs: dictionary with  attributes used to create bridge.
        :param gateway: whether or not the bridge is a gateway.
        :param filtering: whether or not to create filters on the bridge.

        If net_attrs is set, it will add the net_attrs['gateway'] to the bridge
        using net_attrs['broadcast'] and net_attrs['cidr'].  It will also add
        the ip_v6 address specified in net_attrs['cidr_v6'] if use_ipv6 is set.

        The code will attempt to move any IPs that already exist on the
        interface onto the bridge and reset the default gateway if necessary.

        """
        if not device_exists(bridge):
            LOG.debug('Starting Bridge %s', bridge)
            out, err = _execute('ifconfig', 'bridge', 'create', 'name', bridge,
                                check_exit_code=False, run_as_root=True)
            if err and 'File exists' not in err:
                msg = _('Failed to add bridge: %s') % err
                raise exception.NovaException(msg)

            _execute('ifconfig', bridge, 'up', run_as_root=True)

        if interface:
            LOG.debug('Adding interface %(interface)s to bridge %(bridge)s',
                      {'interface': interface, 'bridge': bridge})
            out, err = _execute('ifconfig', bridge, 'addm', interface,
                                check_exit_code=False, run_as_root=True)
            if err and 'File exists' not in err:
                msg = _('Failed to add interface: %s') % err
                raise exception.NovaException(msg)

            # NOTE(apmelton): Linux bridge's default behavior is to use the
            # lowest mac of all plugged interfaces. This isn't a problem when
            # it is first created and the only interface is the bridged
            # interface. But, as instance interfaces are plugged, there is a
            # chance for the mac to change. So, set it here so that it won't
            # change in the future.
            if not CONF.fake_network:
                interface_addrs = netifaces.ifaddresses(interface)
                interface_mac = interface_addrs[netifaces.AF_LINK][0]['addr']
                _execute('ifconfig', bridge, 'ether', interface_mac,
                         run_as_root=True)

            out, err = _execute('ifconfig', interface, 'up',
                                check_exit_code=False, run_as_root=True)

            # NOTE(vish): This will break if there is already an ip on the
            #             interface, so we move any ips to the bridge
            # NOTE(danms): We also need to copy routes to the bridge so as
            #              not to break existing connectivity on the interface
            old_routes = []
            out, err = _execute('netstat', '-nrW', '-f', 'inet')
            for line in out.split('\n'):
                fields = line.split()
                if len(fields) > 6 and (fields[6] == interface) and ('G' in fields[2]):
                    old_routes.append(fields)
                    _execute('route', '-q', 'delete', fields[0], fields[1],
                             run_as_root=True)
            out, err = _execute('ifconfig', interface)
            for line in out.split('\n'):
                fields = line.split()
                if fields and fields[0] == 'inet':
                    _execute(*_ifconfig_tail_cmd(interface, fields, 'delete'),
                             run_as_root=True)
                    _execute(*_ifconfig_tail_cmd(bridge, fields, 'add'),
                             run_as_root=True)
            for fields in old_routes:
                _execute('route', '-q', 'add', fields[0], fields[1],
                         run_as_root=True)

        if filtering:
            # Don't forward traffic unless we were told to be a gateway
            if gateway:
                firewall_manager.ensure_gateway_rules(bridge)
            else:
                firewall_manager.ensure_bridge_rules(bridge)

    @staticmethod
    @utils.synchronized('lock_bridge', external=True)
    def remove_bridge(bridge, gateway=True, filtering=True):
        """Delete a bridge."""
        if not device_exists(bridge):
            return
        else:
            if filtering:
                if gateway:
                    firewall_manager.remove_gateway_rules(bridge)
                else:
                    firewall_manager.remove_bridge_rules(bridge)
            delete_bridge_dev(bridge)


def isolate_dhcp_address(interface, address):
    # block arp traffic to address across the interface
    firewall_manager.ensure_dhcp_isolation(interface, address)


def remove_isolate_dhcp_address(interface, address):
    # block arp traffic to address across the interface
    firewall_manager.remove_dhcp_isolation(interface, address)


# plugs interfaces using Open vSwitch
class FreeBSDOVSInterfaceDriver(FreeBSDNetInterfaceDriver):

    def plug(self, network, mac_address, gateway=True):
        dev = self.get_dev(network)
        if not device_exists(dev):
            bridge = CONF.freebsdnet_ovs_integration_bridge
            _ovs_vsctl(['--', '--may-exist', 'add-port', bridge, dev,
                        '--', 'set', 'Interface', dev, 'type=internal',
                        '--', 'set', 'Interface', dev,
                        'external-ids:iface-id=%s' % dev,
                        '--', 'set', 'Interface', dev,
                        'external-ids:iface-status=active',
                        '--', 'set', 'Interface', dev,
                        'external-ids:attached-mac=%s' % mac_address])
            _execute('ifconfig', dev, 'ether', mac_address, run_as_root=True)
            _set_device_mtu(dev, network.get('mtu'))
            _execute('ifconfig', dev, 'up', run_as_root=True)
            if not gateway:
                # If we weren't instructed to act as a gateway then add the
                # appropriate flows to block all non-dhcp traffic.
                _execute('ovs-ofctl',
                         'add-flow', bridge, 'priority=1,actions=drop',
                         run_as_root=True)
                _execute('ovs-ofctl', 'add-flow', bridge,
                         'udp,tp_dst=67,dl_dst=%s,priority=2,actions=normal' %
                         mac_address, run_as_root=True)
                # .. and make sure iptbles won't forward it as well.
                firewall_manager.ensure_bridge_rules(bridge)
            else:
                firewall_manager.ensure_gateway_rules(bridge)

        return dev

    def unplug(self, network):
        dev = self.get_dev(network)
        bridge = CONF.freebsdnet_ovs_integration_bridge
        _ovs_vsctl(['--', '--if-exists', 'del-port', bridge, dev])
        return dev

    def get_dev(self, network):
        dev = 'gw-' + str(network['uuid'][0:11])
        return dev


# plugs interfaces using FreeBSD Bridge when using NeutronManager
class NeutronFreeBSDBridgeInterfaceDriver(FreeBSDNetInterfaceDriver):

    BRIDGE_NAME_PREFIX = 'brq'
    GATEWAY_INTERFACE_PREFIX = 'gw-'

    def plug(self, network, mac_address, gateway=True):
        dev = self.get_dev(network)
        bridge = self.get_bridge(network)
        if not gateway:
            # If we weren't instructed to act as a gateway then add the
            # appropriate flows to block all non-dhcp traffic.
            # .. and make sure iptbles won't forward it as well.
            firewall_manager.ensure_bridge_rules(bridge)
            return bridge
        else:
            firewall_manager.ensure_gateway_rules(bridge)

        create_tap_dev(dev, mac_address)

        if not device_exists(bridge):
            LOG.debug("Starting bridge %s ", bridge)
            utils.execute('ifconfig', 'bridge', 'create', 'name', bridge, run_as_root=True)
            utils.execute('ifconfig', bridge, 'ether', mac_address, run_as_root=True)
            utils.execute('ifconfig', bridge, 'up', run_as_root=True)
            LOG.debug("Done starting bridge %s", bridge)

            full_ip = '%s/%s' % (network['dhcp_server'],
                                 network['cidr'].rpartition('/')[2])
            utils.execute('ifconfig', bridge, full_ip, 'add', run_as_root=True)

        return dev

    def unplug(self, network):
        dev = self.get_dev(network)
        if not device_exists(dev):
            return None
        else:
            delete_net_dev(dev)
            return dev

    def get_dev(self, network):
        dev = self.GATEWAY_INTERFACE_PREFIX + str(network['uuid'][0:11])
        return dev

    def get_bridge(self, network):
        bridge = self.BRIDGE_NAME_PREFIX + str(network['uuid'][0:11])
        return bridge


class FirewallManager(object):
    def __init__(self, execute=_execute):
        self.execute = execute
        self.apply_deferred = False
        self.anchor = 'org.openstack/%s' % get_binary_name()
        self.rules = {
            "translation": [],
            "filtering": []
        }
        self.is_dirty = False

    def _get_rule_section(self, rule):
        LOG.warning("processing rule: %s" % rule)
        head, tail = rule.split(' ', 1)
        if head in ('nat', 'rdr'):
            return 'translation'
        elif head in ('pass', 'block'):
            return 'filtering'
        else:
            return None

    def add_rule(self, rule):
        cleaned_rule = rule.strip()
        section = self._get_rule_section(cleaned_rule)
        if section:
            if cleaned_rule not in self.rules[section]:
                self.rules[section].append(cleaned_rule)
                self.is_dirty = True
                LOG.warning("Added rule to %s: %s", section, cleaned_rule)

    def remove_rule(self, rule):
        cleaned_rule = rule.strip()
        section = self._get_rule_section(cleaned_rule)
        LOG.warning("Removing rule from %s: %s", section, cleaned_rule)
        if section:
            try:
                self.rules[section].remove(cleaned_rule)
                self.is_dirty = True
            except:
                pass

    def defer_apply_on(self):
        self.apply_deferred = True

    def defer_apply_off(self):
        self.apply_deferred = False
        self.apply()

    def dirty(self):
        return self.is_dirty

    def apply(self):
        if self.apply_deferred:
            return
        if self.dirty():
            self._apply()
        else:
            LOG.debug("Skipping apply due to lack of new rules")

    @utils.synchronized('pfctl', external=True)
    def _apply(self):
        all_lines = []
        all_lines.extend(self.rules['translation'])
        all_lines.extend(self.rules['filtering'])
        all_lines.extend(["\n"])

        self.is_dirty = False
        self.execute("pfctl", "-a", self.anchor, "-f", "-",
                     process_input="\n".join(all_lines),
                     run_as_root=True)
        LOG.warning("FirewallManager.apply completed with success")

    def get_gateway_rules(self, bridge):
        LOG.warning("FirewallManager.get_gateway_rules: "
                    "Please configure rules in pf.conf")
        return []

    def ensure_gateway_rules(self, bridge):
        for rule in self.get_gateway_rules(bridge):
            self.add_rule(rule)

    def remove_gateway_rules(self, bridge):
        for rule in self.get_gateway_rules(bridge):
            self.remove_rule(rule)

    def ensure_bridge_rules(self, bridge):
        LOG.warning("FirewallManager.ensure_bridge_rules: "
                    "Please configure rules in pf.conf")

    def remove_bridge_rules(self, bridge):
        LOG.warning("FirewallManager.remove_bridge_rules: "
                    "Please configure rules in pf.conf")

    def ensure_dhcp_isolation(self, interface, address):
        LOG.warning("FirewallManager.ensure_dhcp_isolation: "
                    "DHCP isolation is not yet implemented")

    def remove_dhcp_isolation(self, interface, address):
        LOG.warning("FirewallManager.remove_dhcp_isolation: "
                    "DHCP isolation is not yet implemented")

    def ensure_in_network_traffic_rules(self, fixed_ip, network):
        LOG.warning("FirewallManager.ensure_in_network_traffic_rules: "
                    "Please configure rules in pf.conf")

    def remove_in_network_traffic_rules(self, fixed_ip, network):
        LOG.warning("FirewallManager.remove_in_network_traffic_rules: "
                    "Please configure rules in pf.conf")

    def floating_forward_rules(self, floating_ip, fixed_ip, device):
        rules = []
        rules.append("rdr inet from any to %s -> %s" % (floating_ip, fixed_ip))

        return rules

    def ensure_floating_rules(self, floating_ip, fixed_ip, device):
        for rule in self.floating_forward_rules(floating_ip, fixed_ip, device):
            self.add_rule(rule)

    def remove_floating_rules(self, floating_ip, fixed_ip, device):
        for rule in self.floating_forward_rules(floating_ip, fixed_ip, device):
            self.remove_rule(rule)

    def add_snat_rule(self, ip_range, is_external=False):
        if CONF.routing_source_ip:
            if is_external:
                if CONF.force_snat_range:
                    snat_range = CONF.force_snat_range
                else:
                    snat_range = []
            else:
                snat_range = ['0.0.0.0/0']
            for dest_range in snat_range:
                if not is_external and CONF.public_interface:
                    firewall_manager.add_rule("nat on %s inet from %s to %s -> %s" %
                                              (CONF.public_interface,
                                               ip_range,
                                               dest_range,
                                               CONF.routing_source_ip))
                else:
                    firewall_manager.add_rule("nat inet from %s to %s -> %s" %
                                              (ip_range,
                                               dest_range,
                                               CONF.routing_source_ip))
            firewall_manager.apply()


firewall_manager = FirewallManager()


def get_firewall_manager():
    return firewall_manager
