import collections
import json
import os.path
import unittest
import copy
from datetime import datetime, timedelta

import boto3
import pykube
import mock
import moto
import yaml
import pytz

from autoscaler import reservations
from autoscaler.cluster import Cluster, ClusterNodeState
from autoscaler.kube import KubePod, KubeNode, KubeResource
from autoscaler.notification import Notifier
import autoscaler.utils as utils


class TestCluster(unittest.TestCase):
    def setUp(self):
        # load dummy kube specs
        dir_path = os.path.dirname(os.path.realpath(__file__))
        with open(os.path.join(dir_path, 'data/busybox.yaml'), 'r') as f:
            self.dummy_pod = yaml.load(f.read())
        with open(os.path.join(dir_path, 'data/ds-pod.yaml'), 'r') as f:
            self.dummy_ds_pod = yaml.load(f.read())
        with open(os.path.join(dir_path, 'data/rc-pod.yaml'), 'r') as f:
            self.dummy_rc_pod = yaml.load(f.read())
        with open(os.path.join(dir_path, 'data/node.yaml'), 'r') as f:
            self.dummy_node = yaml.load(f.read())
            for condition in self.dummy_node['status']['conditions']:
                if condition['type'] == 'Ready' and condition['status'] == 'True':
                    condition['lastHeartbeatTime'] = datetime.now(condition['lastHeartbeatTime'].tzinfo)
            # Convert timestamps to strings to match PyKube
            for condition in self.dummy_node['status']['conditions']:
                condition['lastHeartbeatTime'] = datetime.isoformat(condition['lastHeartbeatTime'])
                condition['lastTransitionTime'] = datetime.isoformat(condition['lastTransitionTime'])


        # this isn't actually used here
        # only needed to create the KubePod object...
        self.api = pykube.HTTPClient(pykube.KubeConfig.from_file('~/.kube/config'))

        # start creating our mock ec2 environment
        self.mocks = [moto.mock_ec2(), moto.mock_autoscaling()]
        for moto_mock in self.mocks:
            moto_mock.start()

        client = boto3.client('autoscaling', region_name='us-west-2')
        self.asg_client = client

        client.create_launch_configuration(
            LaunchConfigurationName='dummy-lc',
            ImageId='ami-deadbeef',
            KeyName='dummy-key',
            SecurityGroups=[
                'sg-cafebeef',
            ],
            InstanceType='t2.medium'
        )

        client.create_auto_scaling_group(
            AutoScalingGroupName='dummy-asg',
            LaunchConfigurationName='dummy-lc',
            MinSize=0,
            MaxSize=10,
            VPCZoneIdentifier='subnet-beefbeef',
            Tags=[
                {
                    'Key': 'KubernetesCluster',
                    'Value': 'dummy-cluster',
                    'PropagateAtLaunch': True
                },
                {
                    'Key': 'KubernetesRole',
                    'Value': 'worker',
                    'PropagateAtLaunch': True
                }
            ]
        )

        # finally our cluster
        self.cluster = Cluster(
            aws_access_key='',
            aws_secret_key='',
            aws_regions=['us-west-2', 'us-east-1', 'us-west-1'],
            azure_client_id='',
            azure_client_secret='',
            azure_subscription_id='',
            azure_tenant_id='',
            azure_legacy_regions=[],
            azure_resource_group_names=[],
            kubeconfig='~/.kube/config',
            idle_threshold=60,
            instance_init_time=60,
            type_idle_threshold=60,
            cluster_name='dummy-cluster',
            notifier=mock.Mock(),
            dry_run=False
        )

    def tearDown(self):
        for moto_mock in self.mocks:
            moto_mock.stop()

    def _spin_up_node(self, launch_time=None):
        return self._spin_up_nodes(1, launch_time=launch_time)[0]

    def _spin_up_nodes(self, count, launch_time=None):
        assert count <= 256
        # spin up dummy ec2 node
        self.asg_client.set_desired_capacity(AutoScalingGroupName='dummy-asg',
                                             DesiredCapacity=count)
        response = self.asg_client.describe_auto_scaling_groups()
        nodes = []
        for i, instance in enumerate(response['AutoScalingGroups'][0]['Instances']):
            instance_id = instance['InstanceId']

            dummy_node = copy.deepcopy(self.dummy_node)
            dummy_node['metadata']['labels']['aws/id'] = instance_id
            dummy_node['metadata']['name'] = '10.0.' + str(i) + '.228'
            node = KubeNode(pykube.Node(self.api, dummy_node))
            node.cordon = mock.Mock(return_value="mocked stuff")
            node.drain = mock.Mock(return_value="mocked stuff")
            node.uncordon = mock.Mock(return_value="mocked stuff")
            node.delete = mock.Mock(return_value="mocked stuff")
            nodes.append(node)
        return nodes

    @staticmethod
    def _create_reservation(name, instances, nodes):
        data = {}
        data['name'] = name
        data['username'] = 'dummy'
        data['id'] = data['username'] + '-' + data['name']
        data['node_selectors'] = '{}'
        data['resources'] = json.dumps({'instances': instances})
        return reservations.Reservation(data, nodes)

    def test_reap_dead_node(self):
        node = copy.deepcopy(self.dummy_node)
        TestInstance = collections.namedtuple('TestInstance', ['launch_time'])
        instance = TestInstance(datetime.now(pytz.utc))

        ready_condition = None
        for condition in node['status']['conditions']:
            if condition['type'] == 'Ready':
                ready_condition = condition
                break
        ready_condition['status'] = 'Unknown'

        ready_condition['lastHeartbeatTime'] = datetime.isoformat(datetime.now(pytz.utc) - timedelta(minutes=30))
        kube_node = KubeNode(pykube.Node(self.api, node))
        kube_node.delete = mock.Mock(return_value="mocked stuff")
        self.cluster.maintain([kube_node], {kube_node.instance_id: instance}, {}, [], [], {})
        kube_node.delete.assert_not_called()

        ready_condition['lastHeartbeatTime'] = datetime.isoformat(datetime.now(pytz.utc) - timedelta(hours=2))
        kube_node = KubeNode(pykube.Node(self.api, node))
        kube_node.delete = mock.Mock(return_value="mocked stuff")
        self.cluster.maintain([kube_node], {kube_node.instance_id: instance}, {}, [], [], {})
        kube_node.delete.assert_called_once_with()

    def test_scale_up_selector(self):
        self.dummy_pod['spec']['nodeSelector'] = {
            'aws/type': 'm4.large'
        }
        pod = KubePod(pykube.Pod(self.api, self.dummy_pod))
        selectors_hash = utils.selectors_to_hash(pod.selectors)
        asgs = self.cluster.autoscaling_groups.get_all_groups([])
        self.cluster.fulfill_pending(asgs, selectors_hash, [pod], [])

        response = self.asg_client.describe_auto_scaling_groups()
        self.assertEqual(len(response['AutoScalingGroups']), 1)
        self.assertEqual(response['AutoScalingGroups'][0]['DesiredCapacity'], 0)

    def test_scale_up(self):
        pod = KubePod(pykube.Pod(self.api, self.dummy_pod))
        selectors_hash = utils.selectors_to_hash(pod.selectors)
        asgs = self.cluster.autoscaling_groups.get_all_groups([])
        self.cluster.fulfill_pending(asgs, selectors_hash, [pod], [])

        response = self.asg_client.describe_auto_scaling_groups()
        self.assertEqual(len(response['AutoScalingGroups']), 1)
        self.assertGreater(response['AutoScalingGroups'][0]['DesiredCapacity'], 0)

    def test_scale_up_notification(self):
        big_pod_spec = copy.deepcopy(self.dummy_pod)
        for container in big_pod_spec['spec']['containers']:
            container['resources']['requests']['cpu'] = '100'
        pod = KubePod(pykube.Pod(self.api, self.dummy_pod))
        big_pod = KubePod(pykube.Pod(self.api, big_pod_spec))
        selectors_hash = utils.selectors_to_hash(pod.selectors)
        asgs = self.cluster.autoscaling_groups.get_all_groups([])
        self.cluster.fulfill_pending(asgs, selectors_hash, [pod, big_pod], [])
        self.cluster.notifier.notify_scale.assert_called_with(mock.ANY, mock.ANY, [pod])

    def test_scale_up_for_default_reservation(self):
        self.dummy_pod['spec']['nodeSelector'] = {
            'openai.org/reservation-id': reservations.DEFAULT_ID
        }
        pod = KubePod(pykube.Pod(self.api, self.dummy_pod))
        selectors_hash = utils.selectors_to_hash(pod.selectors)
        asgs = self.cluster.autoscaling_groups.get_all_groups([])
        self.cluster.autoscaling_timeouts.refresh_timeouts = mock.Mock()
        self.cluster.scale({selectors_hash: [pod]}, [], asgs, {}, [])

        response = self.asg_client.describe_auto_scaling_groups()
        self.assertEqual(len(response['AutoScalingGroups']), 1)
        self.assertGreater(response['AutoScalingGroups'][0]['DesiredCapacity'], 0)

    def test_scale_up_for_reservation(self):
        reservation = self._create_reservation('test', 2, [])
        selectors_hash = utils.selectors_to_hash(reservation.node_selectors)
        asgs = self.cluster.autoscaling_groups.get_all_groups([])
        self.cluster.fulfill_pending(asgs, selectors_hash, [], [reservation])

        response = self.asg_client.describe_auto_scaling_groups()
        self.assertEqual(len(response['AutoScalingGroups']), 1)
        self.assertGreater(response['AutoScalingGroups'][0]['DesiredCapacity'], 0)

    @mock.patch('autoscaler.kube.KubeNode.reservation_id', new_callable=mock.PropertyMock)
    def test_assign_reserved_nodes(self, mock_reservation_id_property):
        def mock_setter(_, obj, value):
            obj.selectors['openai.org/reservation-id'] = value

        def mock_getter(_, obj, obj_type):
            return obj.selectors.get('openai.org/reservation-id')

        mock_reservation_id_property.__set__ = mock_setter
        mock_reservation_id_property.__get__ = mock_getter

        nodes = self._spin_up_nodes(3)
        busy_node = nodes[0]
        pod = KubePod(pykube.Pod(self.api, self.dummy_pod))
        reservation = self._create_reservation('test', 1, nodes)

        pending_reservations = self.cluster.assign_nodes_to_reservations(nodes, {busy_node.name: [pod]}, {reservation.id: reservation})
        self.assertFalse(pending_reservations)
        self.assertIsNone(busy_node.reservation_id)
        self.assertTrue({nodes[1].reservation_id, nodes[2].reservation_id} == {reservations.DEFAULT_ID, reservation.id})

    def test_scale_down(self):
        """
        kube node with daemonset and no pod --> cordon
        """
        node = self._spin_up_node()

        all_nodes = [node]
        managed_nodes = [n for n in all_nodes if node.is_managed()]
        running_insts_map = self.cluster.get_running_instances_map(managed_nodes, [])
        pods_to_schedule = {}
        asgs = self.cluster.autoscaling_groups.get_all_groups(all_nodes)

        ds_pod = KubePod(pykube.Pod(self.api, self.dummy_ds_pod))
        running_or_pending_assigned_pods = [ds_pod]

        self.cluster.idle_threshold = -1
        self.cluster.type_idle_threshold = -1
        self.cluster.LAUNCH_HOUR_THRESHOLD = -1
        self.cluster.maintain(
            managed_nodes, running_insts_map,
            pods_to_schedule, running_or_pending_assigned_pods, asgs,
            {})

        response = self.asg_client.describe_auto_scaling_groups()
        self.assertEqual(len(response['AutoScalingGroups']), 1)
        self.assertEqual(response['AutoScalingGroups'][0]['DesiredCapacity'], 1)
        node.cordon.assert_called_once_with()

    def test_scale_down_launch_grace_period(self):
        """
        kube node with daemonset and no pod + launch grace period --> noop
        """
        node = self._spin_up_node()
        all_nodes = [node]
        managed_nodes = [n for n in all_nodes if node.is_managed()]
        running_insts_map = self.cluster.get_running_instances_map(managed_nodes, [])
        pods_to_schedule = {}
        asgs = self.cluster.autoscaling_groups.get_all_groups(all_nodes)

        ds_pod = KubePod(pykube.Pod(self.api, self.dummy_ds_pod))
        running_or_pending_assigned_pods = [ds_pod]

        self.cluster.idle_threshold = -1
        self.cluster.type_idle_threshold = -1
        self.cluster.maintain(
            managed_nodes, running_insts_map,
            pods_to_schedule, running_or_pending_assigned_pods, asgs,
            {})

        response = self.asg_client.describe_auto_scaling_groups()
        self.assertEqual(len(response['AutoScalingGroups']), 1)
        self.assertEqual(response['AutoScalingGroups'][0]['DesiredCapacity'], 1)
        node.cordon.assert_not_called()

    def test_scale_down_grace_period(self):
        """
        kube node with daemonset and no pod + grace period --> noop
        """
        node = self._spin_up_node()
        all_nodes = [node]
        managed_nodes = [n for n in all_nodes if node.is_managed()]
        running_insts_map = self.cluster.get_running_instances_map(managed_nodes, [])
        pods_to_schedule = {}
        asgs = self.cluster.autoscaling_groups.get_all_groups(all_nodes)

        # kube node with daemonset and no pod --> cordon
        ds_pod = KubePod(pykube.Pod(self.api, self.dummy_ds_pod))
        running_or_pending_assigned_pods = [ds_pod]

        self.cluster.maintain(
            managed_nodes, running_insts_map,
            pods_to_schedule, running_or_pending_assigned_pods, asgs,
            {})

        response = self.asg_client.describe_auto_scaling_groups()
        self.assertEqual(len(response['AutoScalingGroups']), 1)
        self.assertEqual(response['AutoScalingGroups'][0]['DesiredCapacity'], 1)
        node.cordon.assert_not_called()

    def test_scale_down_busy(self):
        """
        kube node with daemonset and pod/rc-pod --> noop
        """
        node = self._spin_up_node()
        all_nodes = [node]
        managed_nodes = [n for n in all_nodes if node.is_managed()]
        running_insts_map = self.cluster.get_running_instances_map(managed_nodes, [])
        pods_to_schedule = {}
        asgs = self.cluster.autoscaling_groups.get_all_groups(all_nodes)

        # kube node with daemonset and pod --> noop
        ds_pod = KubePod(pykube.Pod(self.api, self.dummy_ds_pod))
        pod = KubePod(pykube.Pod(self.api, self.dummy_pod))
        rc_pod = KubePod(pykube.Pod(self.api, self.dummy_rc_pod))

        pod_scenarios = [
            # kube node with daemonset and pod --> noop
            [ds_pod, pod],
            # kube node with daemonset and rc pod --> noop
            [ds_pod, rc_pod]
        ]

        # make sure we're not on grace period
        self.cluster.idle_threshold = -1
        self.cluster.type_idle_threshold = -1

        for pods in pod_scenarios:
            state = self.cluster.get_node_state(
                node, asgs[0], pods, pods_to_schedule,
                running_insts_map, collections.Counter(), {}, {})
            self.assertEqual(state, ClusterNodeState.BUSY)

            self.cluster.maintain(
                managed_nodes, running_insts_map,
                pods_to_schedule, pods, asgs, {})

            response = self.asg_client.describe_auto_scaling_groups()
            self.assertEqual(len(response['AutoScalingGroups']), 1)
            self.assertEqual(response['AutoScalingGroups'][0]['DesiredCapacity'], 1)
            node.cordon.assert_not_called()

    def test_scale_down_under_utilized_undrainable(self):
        """
        kube node with daemonset and pod/rc-pod --> noop
        """
        node = self._spin_up_node()
        all_nodes = [node]
        managed_nodes = [n for n in all_nodes if node.is_managed()]
        running_insts_map = self.cluster.get_running_instances_map(managed_nodes, [])
        pods_to_schedule = {}
        asgs = self.cluster.autoscaling_groups.get_all_groups(all_nodes)

        # create some undrainable pods
        ds_pod = KubePod(pykube.Pod(self.api, self.dummy_ds_pod))
        for container in self.dummy_pod['spec']['containers']:
            container.pop('resources', None)
        pod = KubePod(pykube.Pod(self.api, self.dummy_pod))
        self.dummy_rc_pod['metadata']['labels']['openai/do-not-drain'] = 'true'
        for container in self.dummy_rc_pod['spec']['containers']:
            container.pop('resources', None)
        rc_pod = KubePod(pykube.Pod(self.api, self.dummy_rc_pod))

        pod_scenarios = [
            # kube node with daemonset and pod with no resource ask --> noop
            [ds_pod, pod],
            # kube node with daemonset and critical rc pod --> noop
            [ds_pod, rc_pod]
        ]

        # make sure we're not on grace period
        self.cluster.idle_threshold = -1
        self.cluster.type_idle_threshold = -1
        self.cluster.LAUNCH_HOUR_THRESHOLD = -1

        for pods in pod_scenarios:
            state = self.cluster.get_node_state(
                node, asgs[0], pods, pods_to_schedule,
                running_insts_map, collections.Counter(), {}, {})
            self.assertEqual(state, ClusterNodeState.UNDER_UTILIZED_UNDRAINABLE)

            self.cluster.maintain(
                managed_nodes, running_insts_map,
                pods_to_schedule, pods, asgs, {})

            response = self.asg_client.describe_auto_scaling_groups()
            self.assertEqual(len(response['AutoScalingGroups']), 1)
            self.assertEqual(response['AutoScalingGroups'][0]['DesiredCapacity'], 1)
            node.cordon.assert_not_called()

    def test_scale_down_under_utilized_drainable(self):
        """
        kube node with daemonset and rc-pod --> cordon+drain
        """
        node = self._spin_up_node()
        all_nodes = [node]
        managed_nodes = [n for n in all_nodes if node.is_managed()]
        running_insts_map = self.cluster.get_running_instances_map(managed_nodes, [])
        pods_to_schedule = {}
        asgs = self.cluster.autoscaling_groups.get_all_groups(all_nodes)

        # create some undrainable pods
        ds_pod = KubePod(pykube.Pod(self.api, self.dummy_ds_pod))
        for container in self.dummy_rc_pod['spec']['containers']:
            container.pop('resources', None)
        rc_pod = KubePod(pykube.Pod(self.api, self.dummy_rc_pod))
        pods = [ds_pod, rc_pod]

        # make sure we're not on grace period
        self.cluster.idle_threshold = -1
        self.cluster.type_idle_threshold = -1
        self.cluster.LAUNCH_HOUR_THRESHOLD = -1

        state = self.cluster.get_node_state(
            node, asgs[0], pods, pods_to_schedule,
            running_insts_map, collections.Counter(), {}, {})
        self.assertEqual(state, ClusterNodeState.UNDER_UTILIZED_DRAINABLE)

        self.cluster.maintain(
            managed_nodes, running_insts_map,
            pods_to_schedule, pods, asgs, {})

        response = self.asg_client.describe_auto_scaling_groups()
        self.assertEqual(len(response['AutoScalingGroups']), 1)
        self.assertEqual(response['AutoScalingGroups'][0]['DesiredCapacity'], 1)
        node.cordon.assert_called_once_with()
        node.drain.assert_called_once_with(pods, notifier=mock.ANY)
