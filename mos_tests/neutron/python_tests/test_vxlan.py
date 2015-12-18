#    Copyright 2015 Mirantis, Inc.
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

from contextlib import contextmanager
from distutils.spawn import find_executable
import logging
import subprocess
import threading

import pytest

from mos_tests.neutron.python_tests.base import TestBase


logger = logging.getLogger(__name__)


@contextmanager
def tcpdump_vxlan(ip, env, log_path):
    """Start tcpdump on vxlan port before enter and stop it after

    Log will download to log_path argument
    """
    def tcpdump(ip):
        logger.info('Start tcpdump on {0}'.format(ip))
        with env.get_ssh_to_node(ip) as remote:
            result = remote.execute(
                'tcpdump -U -vvni any port 4789 -w /tmp/vxlan.log')
            assert result['exit_code'] == 0

    thread = threading.Thread(target=tcpdump, args=(ip,))
    try:
        # Start tcpdump
        thread.start()
        yield
    except Exception:
        raise
    else:
        # Download log
        with env.get_ssh_to_node(ip) as remote:
            remote.download('/tmp/vxlan.log', log_path)
    finally:
        # Kill tcpdump
        with env.get_ssh_to_node(ip) as remote:
            remote.execute('killall tcpdump')
        thread.join(0)


def _run_tshark_on_vxlan(log_file, cond):
    tshark = find_executable('tshark')
    return subprocess.check_output([
        tshark, '-d', 'udp.port==4789,vxlan', '-r', log_file,
        '-Y', '{0}'.format(cond)])


def check_all_traffic_has_vni(vni, log_file):
    __tracebackhide__ = True
    output = _run_tshark_on_vxlan(log_file, 'vxlan.vni!={0}'.format(vni))
    if output.strip():
        pytest.fail(
            "Log contains records with another VNI\n{0}".format(output))


def check_no_arp_traffic(src_ip, dst_ip, log_file):
    __tracebackhide__ = True
    cond = ("arp.dst.proto_ipv4=={dst_ip} and "
            "arp.src.proto_ipv4=={src_ip}".format(dst_ip=dst_ip, src_ip=src_ip)
    )
    output = _run_tshark_on_vxlan(log_file, cond)
    if output.strip():
        pytest.fail("Log contains ARP traffic\n{0}".format(output))


def check_icmp_traffic(src_ip, dst_ip, log_file):
    __tracebackhide__ = True
    cond = "icmp and ip.src=={src_ip} and ip.dst=={dst_ip}".format(
        src_ip=src_ip,
        dst_ip=dst_ip
    )
    output = _run_tshark_on_vxlan(log_file, cond)
    if not output.strip():
        pytest.fail(
            "Log not contains ICMP traffic from {src_ip} to {dst_ip}".format(
                src_ip=src_ip,
                dst_ip=dst_ip))


@pytest.mark.check_env_('is_vxlan')
@pytest.mark.usefixtures("setup")
class TestVxlanBase(TestBase):
    """Vxlan (tun) specific tests"""

    @pytest.fixture
    def variables(self, init):
        """Init Openstack variables"""
        self.zone = self.os_conn.nova.availability_zones.find(zoneName="nova")
        self.security_group = self.os_conn.create_sec_group_for_ssh()
        self.instance_keypair = self.os_conn.create_key(key_name='instancekey')

    @pytest.fixture
    def router(self, variables):
        """Make router and connnect it to external network"""
        router = self.os_conn.create_router(name="router01")
        self.os_conn.router_gateway_add(
            router_id=router['router']['id'],
            network_id=self.os_conn.ext_network['id'])
        return router


@pytest.mark.need_tshark
class TestVxlan(TestVxlanBase):
    """Simple Vxlan tests"""

    def test_tunnel_established(self, router):
        """Check that VxLAN is established on nodes and VNI matching
           the segmentation_id of a network

        Scenario:
            1. Create private network net01, subnet 10.1.1.0/24
            2. Create router01, add interface for net01 and set gateway to
                external network
            3. Boot instance vm1_1 in net01
            4. Look on what node l3 agent for this router01 is
            5. Check that tunnel is established on controller
            6. Check that tunnel is established on compute
            7. On node with l3 agent find namespace qrouter
            8. Add rules for ping and ssh connection
            9. Go to the compute with vm_1 and run
                tcpdump -vvni any port 4789 -w vxlan.log
            10. Ping from qrouter namespace vm1
            11. Copy vxlan.log for your computer and open it with Wireshark.
                Press right button, choose Decode as, Transport
                and choose VXLAN
        """
        # Create network and instance
        compute_node = self.zone.hosts.keys()[0]
        network = self.os_conn.create_network(name='net01')
        subnet = self.os_conn.create_subnet(
            network_id=network['network']['id'],
            name='net01__subnet',
            cidr="10.1.1.0/24")
        self.os_conn.router_interface_add(
            router_id=router['router']['id'],
            subnet_id=subnet['subnet']['id'])
        server = self.os_conn.create_server(
            name='server01',
            availability_zone='{}:{}'.format(self.zone.zoneName, compute_node),
            key_name=self.instance_keypair.name,
            nics=[{'net-id': network['network']['id']}],
            security_groups=[self.security_group.id])

        router_node = self.os_conn.get_l3_agent_hosts(
            router['router']['id'])[0]
        controller = self.env.find_node_by_fqdn(router_node)
        compute = self.env.find_node_by_fqdn(compute_node)

        # Check controller and compute
        for node in (controller, compute):
            with self.env.get_ssh_to_node(node.data['ip']) as remote:
                result = remote.execute('ovs-vsctl show | grep -q br-tun')
                assert result['exit_code'] == 0

        with tcpdump_vxlan(ip=compute.data['ip'], env=self.env,
                           log_path='/tmp/vxlan.log'):
            with self.env.get_ssh_to_node(controller.data['ip']) as remote:
                vm_ip = self.os_conn.get_nova_instance_ips(server)['fixed']
                result = remote.execute(
                    'ip netns exec qrouter-{router_id} ping -c1 {ip}'.format(
                        router_id=router['router']['id'],
                        ip=vm_ip))

        # Check log
        vni = network['network']['provider:segmentation_id']
        check_all_traffic_has_vni(vni, '/tmp/vxlan1.log')

    @pytest.mark.check_env_('has_2_or_more_computes')
    def test_vni_for_icmp_between_instances(self, router):
        """Check VNI and segmention_id for icmp traffic between instances
        on different computers

        Scenario:
            1. Create private network net01, subnet 10.1.1.0/24
            2. Create private network net02, subnet 10.1.2.0/24
            3. Create router01_02 and connect net01 and net02 with it
            4. Boot instances vm1 and vm2 on different computers
            5. Check that net02 got a new segmentation_id, different from net1
            6. Ping vm1 from vm2
            7. On compute with vm_1 start listen vxlan port 4789
            8. On compute with vm_2 start listen vxlan port 4789
            9. Ping vm2 from vm1
            10. Check that when traffic goes through net02 tunnel
                (from vm2 to router01_02) all packets have VNI of net02
                and when they travel through net01 tunnel
                (from router to vm1) they have VNI of net01
        """
        # Create network and instance
        compute_nodes = self.zone.hosts.keys()[:2]
        for i, compute_node in enumerate(compute_nodes, 1):
            network = self.os_conn.create_network(name='net%02d' % i)
            subnet = self.os_conn.create_subnet(
                network_id=network['network']['id'],
                name='net%02d__subnet' % i,
                cidr="10.1.%d.0/24" % i)
            self.os_conn.router_interface_add(
                router_id=router['router']['id'],
                subnet_id=subnet['subnet']['id'])
            self.os_conn.create_server(
                name='server%02d' % i,
                availability_zone='{}:{}'.format(self.zone.zoneName,
                                                 compute_node),
                key_name=self.instance_keypair.name,
                nics=[{'net-id': network['network']['id']}],
                security_groups=[self.security_group.id])

        net1, net2 = [x for x in self.os_conn.list_networks()['networks']
                      if x['name'] in ("net01", "net02")]

        # Check that networks has different segmentation_id
        assert (net1['provider:segmentation_id'] !=
                net2['provider:segmentation_id'])

        # Check ping from server1 to server2
        server1 = self.os_conn.nova.servers.find(name="server01")
        server2 = self.os_conn.nova.servers.find(name="server02")
        server2_ip = self.os_conn.get_nova_instance_ips(server2).values()[0]
        self.check_ping_from_vm(server1, self.instance_keypair, server2_ip)

        # Start tcpdump
        compute1 = self.env.find_node_by_fqdn(compute_nodes[0])
        compute2 = self.env.find_node_by_fqdn(compute_nodes[1])
        with tcpdump_vxlan(
                ip=compute1.data['ip'], env=self.env,
                log_path='/tmp/vxlan1.log'
            ), tcpdump_vxlan(
                ip=compute2.data['ip'], env=self.env,
                log_path='/tmp/vxlan2.log'
            ):
            # Ping server1 from server2
            server1_ip = self.os_conn.get_nova_instance_ips(
                server1).values()[0]
            self.check_ping_from_vm(server2, self.instance_keypair, server1_ip)

        # Check traffic
        check_all_traffic_has_vni(net1['provider:segmentation_id'],
                                  '/tmp/vxlan1.log')
        check_all_traffic_has_vni(net2['provider:segmentation_id'],
                                  '/tmp/vxlan2.log')


@pytest.mark.check_env_('is_l2pop')
class TestVxlanL2pop(TestVxlanBase):
    """Vxlan (tun) with enabled L2 population specific tests"""

    @pytest.mark.need_tshark
    @pytest.mark.check_env_('has_2_or_more_computes')
    def test_broadcast_traffic_propagation(self, router):
        """Check broadcast traffic propagation for network segments

        Scenario:
            1. Create private network net01, subnet 10.1.1.0/24
            2. Create private network net02, subnet 10.1.2.0/24
            3. Create router01_02 and connect net01 and net02 with it
            4. Boot instances vm1 in net01 and vm2 in net02
                on different computes
            5. Check that net02 got a new segmentation_id, different from net1
            6. Go to the vm1's console and initiate broadcast traffic to vm2
            7. On the compute where vm2 is hosted start listen vxlan port 4789
            8. Check that no ARP traffic associated with vm1-vm2 pair
                appears on compute node's console
            9. Go to the vm1's console, stop arping and initiate
                unicast traffic to vm2
            10. Check that ICMP unicast traffic associated with vm1-vm2 pair
                was captured on compute node's console
        """
        # Create network and instance
        compute_nodes = self.zone.hosts.keys()[:2]
        for i, compute_node in enumerate(compute_nodes, 1):
            network = self.os_conn.create_network(name='net%02d' % i)
            subnet = self.os_conn.create_subnet(
                network_id=network['network']['id'],
                name='net%02d__subnet' % i,
                cidr="10.1.%d.0/24" % i)
            self.os_conn.router_interface_add(
                router_id=router['router']['id'],
                subnet_id=subnet['subnet']['id'])
            self.os_conn.create_server(
                name='server%02d' % i,
                availability_zone='{}:{}'.format(self.zone.zoneName,
                                                 compute_node),
                key_name=self.instance_keypair.name,
                nics=[{'net-id': network['network']['id']}],
                security_groups=[self.security_group.id])

        net1, net2 = [x for x in self.os_conn.list_networks()['networks']
                      if x['name'] in ("net01", "net02")]

        # Check that networks has different segmentation_id
        assert (net1['provider:segmentation_id'] !=
                net2['provider:segmentation_id'])

        server1 = self.os_conn.nova.servers.find(name="server01")
        server1_ip = self.os_conn.get_nova_instance_ips(server1)['fixed']
        server2 = self.os_conn.nova.servers.find(name="server02")
        server2_ip = self.os_conn.get_nova_instance_ips(server2)['fixed']
        compute2 = self.env.find_node_by_fqdn(compute_nodes[1])

        # Initiate broadcast traffic from server1 to server2
        broadcast_log = '/tmp/vxlan_broadcast.log'
        with tcpdump_vxlan(
                ip=compute2.data['ip'], env=self.env,
                log_path=broadcast_log
            ):
            cmd = 'sudo arping -I eth0 -c 4 {0}; true'.format(server2_ip)
            self.run_on_vm(server1, self.instance_keypair, cmd)

        check_no_arp_traffic(src_ip=server1_ip, dst_ip=server2_ip,
                             log_file=broadcast_log)

        # Initiate unicast traffic from server1 to server2
        unicast_log = '/tmp/vxlan_unicast.log'
        with tcpdump_vxlan(
                ip=compute2.data['ip'], env=self.env,
                log_path=unicast_log
            ):
            cmd = 'ping -c 4 {0}; true'.format(server2_ip)
            self.run_on_vm(server1, self.instance_keypair, cmd)

        check_icmp_traffic(src_ip=server1_ip, dst_ip=server2_ip,
                           log_file=unicast_log)

    @pytest.mark.check_env_('has_3_or_more_computes')
    def test_establishing_tunnels_between_computes(self, variables):
        """Check the tunnels established between computes

        Scenario:
            1. Create net01, net01__subnet, 192.168.1.0/24
            2. Launch vm1 in net01 network on compute1
            3. Go to compute1's console and check that only tunnels
                to controllers appear and no tunnels are to compute2
                and compute3
            4. Go to compute2 and compute3 consoles and check that
                no tunnels appear on them
            5. Launch vm2 in net01 network on compute2
            6. Go to compute2's console and check that tunnels
                to controllers and compute1 appear
            7. Go to compute1's console and check that tunnel
                to compute2 is added
            8. Go to compute3's console and check that no tunnels appear on it
            9. Launch vm3 in net01 network on compute3
            10. Go to compute3's console and check that tunnels to controllers,
                compute1 and compute2 appear
            11. Go to compute1 and compute2 consoles and check that tunnels
                to compute3 are added on them
        """
        # Create net, subnet and server01
        compute_nodes = self.zone.hosts.keys()[:3]

        network = self.os_conn.create_network(name='net01')
        self.os_conn.create_subnet(
            network_id=network['network']['id'],
            name='net01__subnet',
            cidr="192.168.1.0/24")
        self.os_conn.create_server(
            name='server01',
            availability_zone='{}:{}'.format(self.zone.zoneName,
                                             compute_nodes[0]),
            key_name=self.instance_keypair.name,
            nics=[{'net-id': network['network']['id']}],
            security_groups=[self.security_group.id])

        compute1 = self.env.find_node_by_fqdn(compute_nodes[0])
        compute2 = self.env.find_node_by_fqdn(compute_nodes[1])
        compute3 = self.env.find_node_by_fqdn(compute_nodes[2])
        controllers = self.env.get_nodes_by_role('controller')
        # Check that compute1 tunnels only to controller
        with compute1.ssh() as remote:
            result = remote.execute(
                'ovs-vsctl show')
            assert result['exit_code'] == 0
            stdout = ''.join(result['stdout'])
            assert any([x in stdout for c in controllers for x in c.ip_list])
            assert not any([x in stdout for x in compute2.ip_list])
            assert not any([x in stdout for x in compute3.ip_list])

        # Check that compute2 and compute3 have not tunnels
        for node in (compute2, compute3):
            with node.ssh() as remote:
                result = remote.execute(
                    'ovs-vsctl show')
                assert result['exit_code'] == 0
                stdout = ''.join(result['stdout'])
                assert not any([x in stdout for x
                               in ('local_ip', 'remote_ip')])

        # Create server02
        self.os_conn.create_server(
            name='server02',
            availability_zone='{}:{}'.format(self.zone.zoneName,
                                             compute_nodes[1]),
            key_name=self.instance_keypair.name,
            nics=[{'net-id': network['network']['id']}],
            security_groups=[self.security_group.id])

        # Check that compute2 have tunnels to controller abd compute1
        with compute2.ssh() as remote:
            result = remote.execute(
                'ovs-vsctl show')
            assert result['exit_code'] == 0
            stdout = ''.join(result['stdout'])
            assert any([x in stdout for c in controllers for x in c.ip_list])
            assert any([x in stdout for x in compute1.ip_list])

        # Check that compute1 have tunnel to compute2
        with compute2.ssh() as remote:
            result = remote.execute(
                'ovs-vsctl show')
            assert result['exit_code'] == 0
            stdout = ''.join(result['stdout'])
            assert any([x in stdout for x in compute2.ip_list])

        # Check that compute3 haven't tunnels
        with compute3.ssh() as remote:
            result = remote.execute(
                'ovs-vsctl show')
            assert result['exit_code'] == 0
            stdout = ''.join(result['stdout'])
            assert not any([x in stdout for x in ('local_ip', 'remote_ip')])

        # Create server03
        self.os_conn.create_server(
            name='server03',
            availability_zone='{}:{}'.format(self.zone.zoneName,
                                             compute_nodes[2]),
            key_name=self.instance_keypair.name,
            nics=[{'net-id': network['network']['id']}],
            security_groups=[self.security_group.id])

        # Check that compute3 have tunnels to controller, compute1 and compute2
        with compute3.ssh() as remote:
            result = remote.execute(
                'ovs-vsctl show')
            assert result['exit_code'] == 0
            stdout = ''.join(result['stdout'])
            assert any([x in stdout for c in controllers for x in c.ip_list])
            assert any([x in stdout for x in compute2.ip_list])
            assert any([x in stdout for x in compute3.ip_list])

        # Check compute2 and compute3 have tunnels to compute3
        for node in (compute1, compute2):
            with node.ssh() as remote:
                result = remote.execute(
                    'ovs-vsctl show')
                assert result['exit_code'] == 0
                stdout = ''.join(result['stdout'])
                assert any([x in stdout for x in compute3.ip_list])