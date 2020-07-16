import hashlib

from troposphere import autoscaling, ec2, Base64, Parameter, Ref, Sub, iam

import netaddr

from config import constants
from .base import IvyTemplate
from utils.ec2 import EBS_OPTIMIZED_INSTANCES, get_block_device_mapping, get_latest_ami_id


class CassandraTemplate(IvyTemplate):
    CAPABILITIES = ['CAPABILITY_IAM']

    def configure(self):
        """
        Returns a cassandra template with seed nodes
        """
        self.add_description('Sets up Cassandra in all Zones')
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
                Default=get_latest_ami_id(self.region, 'ivy-cassandra', _global_config.get('ami_owner', 'self'))
            )
        )
        _cassandra_security_group = self.add_resource(
            ec2.SecurityGroup(
                '{}SecurityGroup'.format(self.name),
                VpcId=self.vpc_id,
                GroupDescription='Security Group for {} Instances'.format(self.name),
                SecurityGroupIngress=[
                    {'IpProtocol': 'tcp', 'FromPort': 7000, 'ToPort': 7001, 'CidrIp': self.vpc_cidr},  # inter-node
                    {'IpProtocol': 'tcp', 'FromPort': 7199, 'ToPort': 7199, 'CidrIp': self.vpc_cidr},  # jmx
                    {'IpProtocol': 'tcp', 'FromPort': 9042, 'ToPort': 9042, 'CidrIp': self.vpc_cidr},  # client port
                    {'IpProtocol': 'tcp', 'FromPort': 9160, 'ToPort': 9160, 'CidrIp': self.vpc_cidr},  # client (thrift)
                ]
            )
        )
        self.add_resource(
            ec2.SecurityGroupIngress(
                '{}IngressSecurityGroup'.format(self.name),
                GroupId=Ref(_cassandra_security_group),
                IpProtocol='-1',
                FromPort=-1,
                ToPort=-1,
                SourceSecurityGroupId=Ref(_cassandra_security_group)  # this allows members all traffic
            )
        )
        self.add_security_group(Ref(_cassandra_security_group))

        # Add support for creating EBS snapshots and tagging them
        self.add_iam_policy(iam.Policy(
            PolicyName='CassandraBackups',
            PolicyDocument={
                'Statement': [{
                    'Effect': 'Allow',
                    'Resource': '*',
                    'Action': [
                        'ec2:AttachVolume',
                        'ec2:CreateSnapshot',
                        'ec2:CreateTags',
                        'ec2:DeleteSnapshot',
                        'ec2:DescribeInstances',
                        'ec2:DescribeSnapshots',
                        'ec2:DescribeTags',
                        'ec2:DescribeVolumeAttribute',
                        'ec2:DescribeVolumeStatus',
                        'ec2:DescribeVolumes',
                        'ec2:DetachVolume'
                    ]
                }]
            }
        ))

        for cluster in constants.ENVIRONMENTS[self.env]['cassandra']['clusters']:
            for _instance in cluster['instances']:

                subnet = [s for s in self.get_subnets('private') if netaddr.IPAddress(_instance['ip']) in netaddr.IPNetwork(s['CidrBlock'])][0]

                service = 'cassandra-{}'.format(cluster['name'])
                role = '-'.join([self.name, cluster['name'], subnet['AvailabilityZone'], _instance['ip']])
                tags = self.get_tags(service_override=service, role_override=role)

                # Create ENI for this server, and hold onto a Ref for it so we can feed it into the userdata
                uniq_id = hashlib.md5(role.encode('utf-8')).hexdigest()[:10]
                eni = ec2.NetworkInterface(
                    self.name + cluster['name'] + "ENI" + uniq_id,
                    Description='Cassandra: Cluster: {} ENV: {} PrivateSubnet {}'.format(
                        cluster['name'], self.env, subnet['SubnetId']),
                    GroupSet=self.security_groups,
                    PrivateIpAddress=_instance['ip'],
                    SourceDestCheck=True,
                    SubnetId=subnet['SubnetId'],
                    Tags=tags,
                    )
                self.add_resource(eni)

                # Add the rootfs
                _block_device_mapping = get_block_device_mapping(self.parameters['InstanceType'].resource['Default'])
                _block_device_mapping += {
                    ec2.BlockDeviceMapping(
                        DeviceName="/dev/xvda",
                        Ebs=ec2.EBSBlockDevice(
                            DeleteOnTermination=True,
                            VolumeSize=cluster.get('rootfs_size', 20),
                            VolumeType="gp2",
                        )
                    )
                }

                # Seed the cluster from one node in the remote DC, plus three nodes in this DC
                # We want to avoid making too many nodes into seeds
                if cluster.get('remote_seed'):
                    remote_env_name = cluster['remote_seed']['datacenter']
                    remote_cluster_name = cluster['remote_seed']['cluster']
                    remote_clusters = constants.ENVIRONMENTS[remote_env_name]['cassandra']['clusters']
                    # filter to just the remote cluster in the remote DC and return that one only
                    remote_cluster = list(filter(lambda x: x['name'] == remote_cluster_name, remote_clusters))[0]
                    remote_seeds = [i['ip'] for i in remote_cluster['instances']][:1]
                    local_seeds = [i['ip'] for i in cluster['instances']][:3]
                    seeds = ','.join(remote_seeds + local_seeds)
                else:
                    # Use the first three cassandra nodes as seeds
                    seeds = ','.join([i['ip'] for i in cluster['instances']][:3])

                if cluster.get('data_volume_size'):
                    # Create the EBS volume
                    data_volume = ec2.Volume(
                        '{}{}DataVolume{}'.format(self.name, cluster['name'], uniq_id),  # something like 'envnameCassandraappDataVolumec47145e176'
                        Size=cluster.get('data_volume_size', 20),
                        VolumeType='gp2',
                        AvailabilityZone=subnet['AvailabilityZone'],
                        DeletionPolicy='Retain',
                        Tags=tags + [ec2.Tag('Name', role + "-datavol")]
                    )
                    self.add_resource(data_volume)
                else:
                    data_volume = None

                # Create the user data in two phases
                # Phase 1: substitute from constants in Rain
                user_data_template = self.get_cloudinit_template(
                    cluster['cassandra_template'],
                    replacements=(
                        ('__PROMPT_COLOR__', self.prompt_color()),
                        ('__CASSANDRA_CLUSTER__', cluster['name'] ),
                        ('__CASSANDRA_CLUSTER_OVERRIDE__', cluster.get('cluster_name_override', "") ),
                        ('__CASSANDRA_SEEDS__', seeds),
                        ('__SERVICE__', service)
                    )
                )
                # Phase 2: Allow AWS Cloudformation to further substitute Ref()'s in the userdata
                userdata = Base64(Sub(
                    user_data_template
                        .replace('${', '${!')  # Replace bash brackets with CFN escaped style
                        .replace('{#', '${'),  # Replace rain-style CFN escapes with proper CFN brackets
                    {
                        'CFN_ENI_ID': Ref(eni),
                        'CFN_DATA_EBS_VOLUME_ID': Ref(data_volume) if data_volume else ""
                    }
                ))

                # Create the Launch Configuration / ASG
                _instance_type = cluster.get('instance_type', Ref(self.instance_type))
                launch_configuration = self.add_resource(
                    autoscaling.LaunchConfiguration(
                        '{}{}LaunchConfiguration{}'.format(self.name, cluster['name'], uniq_id),
                        AssociatePublicIpAddress=False,
                        BlockDeviceMappings=_block_device_mapping,
                        EbsOptimized=True if _instance_type in EBS_OPTIMIZED_INSTANCES else False,
                        ImageId=Ref(self.ami),
                        InstanceType=_instance_type,
                        InstanceMonitoring=False,
                        IamInstanceProfile=Ref(self.instance_profile),
                        KeyName=Ref(self.keypair_name),
                        SecurityGroups=self.security_groups,
                        UserData=userdata
                    )
                )
                self.add_resource(
                    autoscaling.AutoScalingGroup(
                        '{}{}ASGroup{}'.format(self.name, cluster['name'], uniq_id),
                        AvailabilityZones=[subnet['AvailabilityZone']],
                        HealthCheckType='EC2',
                        LaunchConfigurationName=Ref(launch_configuration),
                        MinSize=1,
                        MaxSize=1,
                        VPCZoneIdentifier=[subnet['SubnetId']],
                        Tags=self.get_autoscaling_tags(service_override=service, role_override=role) + [
                            autoscaling.Tag('Name', role, True)
                        ]
                    )
                )

