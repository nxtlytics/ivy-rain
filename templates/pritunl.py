import random

from troposphere import autoscaling, ec2, iam, route53, Base64, GetAtt, Parameter, Ref, Sub

from config import constants
from .base import IvyTemplate
from utils.ec2 import get_latest_ami_id


class PritunlTemplate(IvyTemplate):
    CAPABILITIES = ['CAPABILITY_IAM']

    def configure(self):
        """
        Returns a Pritunl template
        """
        self.defaults = {
            'instance_type': 't3.large'
        }

        self.service = 'pritunl'
        self.set_description('Sets up Pritunl servers')
        self.get_default_security_groups()
        self.get_standard_parameters()
        self.get_standard_policies()

        _vpn_config = constants.ENVIRONMENTS[self.env]['pritunl']
        _global_config = constants.ENVIRONMENTS[self.env]
        _bootstrap_mode = _vpn_config.get('bootstrap_mode', False)

        _bootstrap_ami = get_latest_ami_id(self.region, 'amzn2-ami-hvm-2.0.????????-x86_64-gp2', 'amazon')
        _ivy_ami = get_latest_ami_id(self.region, 'ivy-base', _global_config.get('ami_owner', 'self'))

        self.ami = self.add_parameter(
            Parameter(
                'AMI',
                Type='String',
                Description='AMI ID for instances',
                Default=_bootstrap_ami if _bootstrap_mode else _ivy_ami
            )
        )

        _public_dns = _vpn_config['public_dns']

        _vpn_name = '{}Pritunl'.format(self.env)

        # We want the preferred subnet only.
        _vpn_subnet = self.get_subnets('public', _preferred_only=True)[0]

        # Add our security group
        _vpn_security_group = self.add_resource(
            ec2.SecurityGroup(
                '{}SecurityGroup'.format(_vpn_name),
                VpcId=self.vpc_id,
                GroupDescription='Security Group for Pritunl {}'.format(_vpn_name),
                SecurityGroupIngress=[
                    {"IpProtocol": "icmp", "FromPort": "-1", "ToPort": "-1", "CidrIp": "0.0.0.0/0"},  # Ping
                    {"IpProtocol": "tcp", "FromPort": "80", "ToPort": "80", "CidrIp": "0.0.0.0/0"},  # HTTP
                    {"IpProtocol": "tcp", "FromPort": "443", "ToPort": "443", "CidrIp": "0.0.0.0/0"},  # HTTPS
                    {"IpProtocol": "tcp", "FromPort": "22", "ToPort": "22", "CidrIp": "0.0.0.0/0"},  # SSH
                    {"IpProtocol": "udp", "FromPort": "10000", "ToPort": "20000", "CidrIp": "0.0.0.0/0"},  # HTTPS/OVPN
                    {"IpProtocol": "tcp", "FromPort": "27017", "ToPort": "27017", "CidrIp": constants.SUPERNET},  # mongodb master
                    {"IpProtocol": "-1", "FromPort": "-1", "ToPort": "-1", "CidrIp": constants.SUPERNET}  # Replies from local VPC
                ],
                SecurityGroupEgress=[
                    {"IpProtocol": "-1", "FromPort": "-1", "ToPort": "-1", "CidrIp": "0.0.0.0/0"}
                ]
            )
        )

        # Add EBS volume if local mongo used
        _data_volume = None
        if _vpn_config.get('local_mongo', False):
            self.add_iam_policy(iam.Policy(
                PolicyName='AttachVolume',
                PolicyDocument={
                    'Statement': [{
                        'Effect': 'Allow',
                        'Resource': '*',
                        'Action': [
                            'ec2:AttachVolume',
                            'ec2:DeleteSnapshot',
                            'ec2:DescribeTags',
                            'ec2:DescribeVolumeAttribute',
                            'ec2:DescribeVolumeStatus',
                            'ec2:DescribeVolumes',
                            'ec2:DetachVolume'
                        ]
                    }]
                }
            ))
            _data_volume = ec2.Volume(
                '{}DataVolume'.format(_vpn_name),
                Size=_vpn_config.get('data_volume_size', 20),
                VolumeType='gp2',
                AvailabilityZone=_vpn_subnet['AvailabilityZone'],
                DeletionPolicy='Retain',
                Tags=self.get_tags(service_override=self.service, role_override=_vpn_name) + [ec2.Tag('Name', _vpn_name + "-datavol")]
            )
            self.add_resource(_data_volume)

        # Add the elastic IP and the ENI for it, then attach it.
        _vpn_eip = self.add_resource(
            ec2.EIP(
                '{}InstanceEIP'.format(_vpn_name),
                Domain='vpc'
            )
        )
        _vpn_eni = self.add_resource(
            ec2.NetworkInterface(
                '{}InstanceENI'.format(_vpn_name),
                SubnetId=_vpn_subnet['SubnetId'],
                Description='ENI for {}'.format(_vpn_name),
                GroupSet=[Ref(_vpn_security_group)] + self.security_groups,
                SourceDestCheck=False,
                Tags=self.get_tags(service_override=self.service, role_override=_vpn_name)
            )
        )
        self.get_eni_policies()

        self.add_resource(
            ec2.EIPAssociation(
                '{}AssociateVPNInstanceENI'.format(_vpn_name),
                AllocationId=GetAtt(_vpn_eip, "AllocationId"),
                NetworkInterfaceId=Ref(_vpn_eni)
            )
        )

        # Add a route53 DNS name
        if self.get_partition() != 'aws-us-gov':
            self.add_resource(
                route53.RecordSetGroup(
                    '{}Route53'.format(_vpn_name),
                    HostedZoneName=constants.ENVIRONMENTS[self.env]['route53_zone'],
                    RecordSets=[
                        route53.RecordSet(
                            Name=_public_dns,
                            ResourceRecords=[Ref(_vpn_eip)],
                            Type='A',
                            TTL=600
                        )
                    ]
                )
            )

        # Get all route tables in the VPC
        _vpc_route_tables = self.ec2_conn.describe_route_tables(
            Filters=[{'Name': 'vpc-id', 'Values': [self.vpc_id]}])['RouteTables']

        # Set up the routing table for the VPC
        # Allow for changing client subnets in constants.py
        for client_subnet in _vpn_config['client_subnets']:
            for route_table in _vpc_route_tables:
                self.add_resource(
                    ec2.Route(
                        '{}Route{}{}'.format(_vpn_name,
                                                    client_subnet.translate({ord("."): "", ord("/"): ""}),
                                                    route_table['RouteTableId'].replace('-', '')
                                                    ),
                        RouteTableId=route_table['RouteTableId'],
                        DestinationCidrBlock=client_subnet,
                        NetworkInterfaceId=Ref(_vpn_eni)
                    )
                )

        _mongodb = _vpn_config.get('mongodb')
        _server_id = _vpn_config['server_id']

        _userdata_template = self.get_cloudinit_template(_tpl_name="pritunl_bootstrap" if _bootstrap_mode else None,
                                                         replacements=(
            ('__PROMPT_COLOR__', self.prompt_color()),
            ('__SERVER_ID__', _server_id),
            ('__SERVICE__', self.service),
            ('__MONGODB__', _mongodb if _mongodb else '')
        ))

        _userdata = Sub(
            _userdata_template
                .replace('${', '${!')  # Replace bash brackets with CFN escaped style
                .replace('{#', '${'),  # Replace rain-style CFN escapes with proper CFN brackets
            {
                'CFN_ENI_ID': Ref(_vpn_eni),
                'CFN_EBS_ID': Ref(_data_volume) if _data_volume else ''
            }
        )

        _vpn_launch_configuration = self.add_resource(
            autoscaling.LaunchConfiguration(
                '{}LaunchConfiguration'.format(_vpn_name),
                AssociatePublicIpAddress=True,
                KeyName=Ref(self.keypair_name),
                ImageId=Ref(self.ami),
                InstanceType=Ref(self.instance_type),
                InstanceMonitoring=False,
                IamInstanceProfile=Ref(self.instance_profile),
                UserData=Base64(_userdata)
            )
        )
        self.add_resource(
            autoscaling.AutoScalingGroup(
                '{}ASGroup'.format(_vpn_name),
                AvailabilityZones=[_vpn_subnet['AvailabilityZone']],
                HealthCheckType='EC2',
                LaunchConfigurationName=Ref(_vpn_launch_configuration),
                MinSize=0,
                MaxSize=1,
                VPCZoneIdentifier=[_vpn_subnet['SubnetId']],
                Tags=self.get_autoscaling_tags(service_override=self.service, role_override=_vpn_name) + [
                    autoscaling.Tag('Name', _vpn_name, True)
                ]
            )
        )
