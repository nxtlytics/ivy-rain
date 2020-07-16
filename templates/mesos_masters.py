from troposphere import autoscaling, ec2, iam, Base64, Parameter, Ref, Sub

import netaddr
from config import constants
from .base import IvyTemplate
from utils.ec2 import get_block_device_mapping, get_latest_ami_id


class MesosMastersTemplate(IvyTemplate):
    CAPABILITIES = ['CAPABILITY_IAM']

    def configure(self):
        """
        This template creates a mesos-master per subnet in the VPC
        """
        config = constants.ENVIRONMENTS[self.env]['mesos']['master']
        self.defaults = {
            'instance_type': config.get('instance_type', 't3.large')
        }

        self.add_description('Sets up Mesos Masters in all Zones')
        self.get_eni_policies()
        self.get_default_security_groups()
        self.get_standard_parameters()
        self.get_standard_policies()

        _global_config = constants.ENVIRONMENTS[self.env]

        self.ami = self.add_parameter(
            Parameter(
                'AMI',
                Type='String',
                Description='AMI ID for instances',
                Default=get_latest_ami_id(self.region, 'ivy-mesos', _global_config.get('ami_owner', 'self'))
            )
        )
        _mesos_master_security_group = self.add_resource(
            ec2.SecurityGroup(
                'MesosMasterSecurityGroup',
                VpcId=self.vpc_id,
                GroupDescription='Security Group for MesosMaster Instances',
                SecurityGroupIngress=[
                    {'IpProtocol': 'tcp', 'FromPort': 2181, 'ToPort': 2181, 'CidrIp': self.vpc_cidr},  # zk
                    {'IpProtocol': 'tcp', 'FromPort': 4400, 'ToPort': 4400, 'CidrIp': self.vpc_cidr},  # chronos
                    {'IpProtocol': 'tcp', 'FromPort': 5050, 'ToPort': 5051, 'CidrIp': self.vpc_cidr},  # mesos
                    {'IpProtocol': 'tcp', 'FromPort': 8080, 'ToPort': 8080, 'CidrIp': self.vpc_cidr},  # marathon
                    {'IpProtocol': 'tcp', 'FromPort': 8500, 'ToPort': 8500, 'CidrIp': self.vpc_cidr},  # consul ui
                    {'IpProtocol': 'tcp', 'FromPort': 8300, 'ToPort': 8301, 'CidrIp': self.vpc_cidr},  # consul rpc/lan serf
                    {'IpProtocol': 'tcp', 'FromPort': 8302, 'ToPort': 8302, 'CidrIp': constants.SUPERNET},  # consul wan serf
                    {'IpProtocol': 'udp', 'FromPort': 8300, 'ToPort': 8301, 'CidrIp': self.vpc_cidr},  # consul rpc/lan serf (udp)
                    {'IpProtocol': 'udp', 'FromPort': 8302, 'ToPort': 8302, 'CidrIp': constants.SUPERNET},  # consul wan serf (udp)
                ],
                SecurityGroupEgress=[
                    {'IpProtocol': '-1', 'FromPort': 0, 'ToPort': 65535, 'CidrIp': '0.0.0.0/0'}
                ]
            )
        )
        self.add_resource(
            ec2.SecurityGroupIngress(
                'MesosMasterIngressSecurityGroup',
                GroupId=Ref(_mesos_master_security_group),
                IpProtocol='-1',
                FromPort=-1,
                ToPort=-1,
                SourceSecurityGroupId=Ref(_mesos_master_security_group)
                # this allows members all traffic (for replication)
            )
        )
        self.add_security_group(Ref(_mesos_master_security_group))

        masters = [(index, ip) for index, ip in enumerate(config['masters'], 1)]
        subnets = self.get_subnets('private')
        for master in masters:
            zone_index, master_ip = master
            subnet = [s for s in subnets if netaddr.IPAddress(master_ip) in netaddr.IPNetwork(s['CidrBlock'])][0]

            _mesos_master_eni = ec2.NetworkInterface(
                'MesosMasterInstanceENI{}'.format(subnet['AvailabilityZone'][-1]),
                Description='ENI for Mesos Master ENV: {0}  PrivateSubnet {1}'.format(self.env, subnet['SubnetId']),
                GroupSet=self.security_groups,
                PrivateIpAddress=master_ip,
                SourceDestCheck=True,
                SubnetId=subnet['SubnetId'],
                Tags=self.get_tags(service_override="Mesos",
                                   role_override='MesosMaster-{}'.format(subnet['AvailabilityZone']))
            )
            self.add_resource(_mesos_master_eni)

            _user_data_template = self.get_cloudinit_template(
                replacements=(
                    ('__PROMPT_COLOR__', self.prompt_color()),
                    ('__ENI_IP__', master_ip),
                    ('__ZK_SERVER_ID__', zone_index),
                    ('__HOSTS_ENTRIES__', '\n'.join(
                        ['{0} mesos-master-{1}.node.{2}.{3} mesos-master-{1}'.
                             format(ip, index, self.env, constants.TAG) for index, ip in masters]
                    )),
                    ('__ZK_CONNECT__', ','.join(['{}:2181'.format(z[1]) for z in masters])),
                    ('__ZK_PEERS__', '\n'.join([
                        'server.{0}={1}:2888:3888'.format(index, ip) for index, ip in masters
                    ]))

                )
            )

            _user_data = Sub(
                _user_data_template
                    .replace('${', '${!')  # Replace bash brackets with CFN escaped style
                    .replace('{#', '${'),  # Replace rain-style CFN escapes with proper CFN brackets
                {
                    'CFN_ENI_ID': Ref(_mesos_master_eni),
                }
            )

            _mesos_master_launch_configuration = self.add_resource(
                autoscaling.LaunchConfiguration(
                    'MesosMasterLaunchConfiguration{}'.format(subnet['AvailabilityZone'][-1]),
                    AssociatePublicIpAddress=False,
                    BlockDeviceMappings=get_block_device_mapping(self.parameters['InstanceType'].resource['Default']),
                    SecurityGroups=self.security_groups,
                    KeyName=Ref(self.keypair_name),
                    ImageId=Ref(self.ami),
                    InstanceType=Ref(self.instance_type),
                    InstanceMonitoring=False,
                    IamInstanceProfile=Ref(self.instance_profile),
                    UserData=Base64(_user_data)
                )
            )
            self.add_resource(
                autoscaling.AutoScalingGroup(
                    'MesosMasterASGroup{}'.format(subnet['AvailabilityZone'][-1]),
                    AvailabilityZones=[subnet['AvailabilityZone']],
                    HealthCheckType='EC2',
                    LaunchConfigurationName=Ref(_mesos_master_launch_configuration),
                    MinSize=0,
                    MaxSize=1,
                    # DesiredCapacity=1,
                    VPCZoneIdentifier=[subnet['SubnetId']],
                    Tags=self.get_autoscaling_tags(
                        service_override="MesosMaster",
                        role_override='MesosMaster-{}'.format(subnet['AvailabilityZone'])) + [
                             autoscaling.Tag('Name', '{}Mesos-Master-{}'.format(self.env, subnet['AvailabilityZone']),
                                             True),
                             # tag to allow consul to discover the hosts
                             # autoscaling.Tag('{}:consul_master'.format(constants.TAG), self.env, True)
                         ]
                )
            )
