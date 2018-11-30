# Copyright (c) 2018 Tigera, Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
import os
import subprocess
from time import sleep

from kubernetes import client, config

from tests.k8st.test_base import TestBase
from tests.k8st.utils.utils import start_external_node_with_bgp, retry_until_success, run

_log = logging.getLogger(__name__)


bird_conf = """
router id 10.192.0.5;

# Configure synchronization between routing tables and kernel.
protocol kernel {
  learn;             # Learn all alien routes from the kernel
  persist;           # Don't remove routes on bird shutdown
  scan time 2;       # Scan kernel routing table every 2 seconds
  import all;
  export all;
  graceful restart;  # Turn on graceful restart to reduce potential flaps in
                     # routes when reloading BIRD configuration.  With a full
                     # automatic mesh, there is no way to prevent BGP from
                     # flapping since multiple nodes update their BGP
                     # configuration at the same time, GR is not guaranteed to
                     # work correctly in this scenario.
  merge paths on;
}

# Watch interface up/down events.
protocol device {
  debug { states };
  scan time 2;    # Scan interfaces every 2 seconds
}

protocol direct {
  debug { states };
  interface -"cali*", "*"; # Exclude cali* but include everything else.
}

# Template for all BGP clients
template bgp bgp_template {
  debug { states };
  description "Connection to BGP peer";
  local as 64512;
  multihop;
  gateway recursive; # This should be the default, but just in case.
  import all;        # Import all routes, since we don't know what the upstream
                     # topology is and therefore have to trust the ToR/RR.
  export all;
  source address 10.192.0.5;  # The local address we use for the TCP connection
  add paths on;
  graceful restart;  # See comment in kernel section about graceful restart.
  connect delay time 2;
  connect retry time 5;
  error wait time 5,30;
}

# ------------- Node-to-node mesh -------------
# For peer /host/kube-master/ip_addr_v4
protocol bgp Mesh_10_192_0_2 from bgp_template {
  neighbor 10.192.0.2 as 64512;
  passive on; # Mesh is unidirectional, peer will connect to us.
}


# For peer /host/kube-node-1/ip_addr_v4
protocol bgp Mesh_10_192_0_3 from bgp_template {
  neighbor 10.192.0.3 as 64512;
  passive on; # Mesh is unidirectional, peer will connect to us.
}

# For peer /host/kube-node-2/ip_addr_v4
protocol bgp Mesh_10_192_0_4 from bgp_template {
  neighbor 10.192.0.4 as 64512;
  passive on; # Mesh is unidirectional, peer will connect to us.
}
"""


class TestBGPAdvert(TestBase):
    def setUp(self):
        super(TestBGPAdvert, self).setUp()

        # Create bgp test namespace
        self.ns = "bgp-test"
        self.create_namespace(self.ns)

        start_external_node_with_bgp("kube-node-extra", bird_conf)

        # set CALICO_ADVERTISE_CLUSTER_IPS=10.96.0.0/12
        self.update_ds_env("calico-node", "kube-system", "CALICO_ADVERTISE_CLUSTER_IPS", "10.96.0.0/12")

        # Establish BGPPeer from cluster nodes to node-extra using calicoctl
        run("""kubectl exec -i -n kube-system calicoctl -- /calicoctl apply -f - << EOF
apiVersion: projectcalico.org/v3
kind: BGPPeer
metadata:
  name: node-extra.peer
spec:
  peerIP: 10.192.0.5
  asNumber: 64512
EOF
""")

    def tearDown(self):
        try:
            run("docker rm -f kube-node-extra")
        except subprocess.CalledProcessError:
            pass
        self.delete_and_confirm(self.ns, "ns")

    def get_svc_cluster_ip(self, svc, ns):
        return run("kubectl get svc %s -n %s -o json | jq -r .spec.clusterIP" % (svc, ns)).strip()

    def do_external_curl(self, container, hostname):
        return run("docker exec %s curl --connect-timeout 2 -m 3 %s" % (container, hostname))

    def test_mainline(self):
        """
        Runs the mainline tests for service ip advertisement
        - Create both a Local and a Cluster type NodePort service with a single replica.
          - assert only local and service CIDR routes are advertised.
          - assert /32 routes are used, source IP is preserved.
        - Create a local LoadBalancer service with clusterIP = None, assert no change.
        - Scale the Local NP service so it is running on multiple nodes, assert ECMP routing, source IP is preserved.
        - Delete both services, assert only cluster CIDR route is advertised.
        """
        local_svc = "nginx-local"
        cluster_svc = "nginx-cluster"
        # Assert that a route to the service IP range is present
        retry_until_success(lambda: self.assertIn("10.96.0.0/12", self.get_routes()))

        # Create nginx deployment and service
        self.create_service("nginx:1.7.9", local_svc, self.ns, 80)
        self.create_service("nginx:1.7.9", cluster_svc, self.ns, 80, traffic_policy="Cluster")
        self.wait_until_exists(local_svc, "svc", self.ns)
        self.wait_until_exists(cluster_svc, "svc", self.ns)

        # Get clusterIPs
        local_svc_ip = self.get_svc_cluster_ip(local_svc, self.ns)
        cluster_svc_ip = self.get_svc_cluster_ip(cluster_svc, self.ns)

        # Assert that both nginx service can be curled from the external node
        retry_until_success(self.do_external_curl, function_args=["kube-node-extra", local_svc_ip])
        retry_until_success(self.do_external_curl, function_args=["kube-node-extra", cluster_svc_ip])

        # Assert that local clusterIP is an advertised route and cluster clusterIP is not
        retry_until_success(lambda: self.assertIn(local_svc_ip, self.get_routes()))
        retry_until_success(lambda: self.assertNotIn(cluster_svc_ip, self.get_routes()))

        # Delete nginx service
        self.delete_and_confirm(local_svc, "svc", self.ns)
        self.delete_and_confirm(cluster_svc, "svc", self.ns)

        # Assert that clusterIP is no longer and advertised route
        retry_until_success(lambda: self.assertNotIn(local_svc_ip, self.get_routes()))
