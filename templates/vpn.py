from troposphere import autoscaling, ec2, Sub, Base64, GetAtt, Parameter, Ref
import itertools

from config import constants
from .base import IvyTemplate
from utils.ec2 import get_latest_ami_id

class VPNTemplate(IvyTemplate):
    CAPABILITIES = ['CAPABILITY_IAM']

    def configure(self):
        """
        Returns a vpn template
        """
        self.defaults = {
            'instance_type': 't2.small'
        }

        self.service = 'vpn'
        self.add_description('Sets up VPNs')
        self.get_eni_policies()
        self.get_default_security_groups()
        self.get_standard_parameters()
        self.get_standard_policies()
        self.ami = self.add_parameter(
            Parameter(
                'AMI',
                Type='String',
                Description='AMI ID for instances',
                Default=get_latest_ami_id(self.region, 'amzn2-ami-hvm-2.0.????????-x86_64-gp2', 'amazon')
            )
        )

        # Custom config per VPN
        for vpn in constants.ENVIRONMENTS[self.env]['vpn']:
            if not vpn['active']:
                continue
            _vpn_name = vpn['name']
            _vpn_subnet = self.get_subnets('public', _preferred_only=True)[0]
            _role = 'vpn-{}'.format(_vpn_name)

            _vpn_security_group = self.add_resource(
                ec2.SecurityGroup(
                    self.cfn_name('VPNSecurityGroup', _vpn_name),
                    VpcId=self.vpc_id,
                    GroupDescription='Security Group for VPN {}'.format(_vpn_name),
                    SecurityGroupIngress=[
                        {"IpProtocol": "50", "FromPort": "-1", "ToPort": "-1", "CidrIp": vpn['remote_ip'] + '/32'},
                        {"IpProtocol": "51", "FromPort": "-1", "ToPort": "-1", "CidrIp": vpn['remote_ip'] + '/32'},
                        {"IpProtocol": "udp", "FromPort": "500", "ToPort": "500", "CidrIp": vpn['remote_ip'] + '/32'},
                        {"IpProtocol": "udp", "FromPort": "4500", "ToPort": "4500", "CidrIp": vpn['remote_ip'] + '/32'},
                        {"IpProtocol": "icmp", "FromPort": "-1", "ToPort": "-1", "CidrIp": "0.0.0.0/0"},
                        {"IpProtocol": "-1", "FromPort": "-1", "ToPort": "-1", "CidrIp": constants.SUPERNET}
                    ],
                    SecurityGroupEgress=[
                        {"IpProtocol": "50", "FromPort": "-1", "ToPort": "-1", "CidrIp": vpn['remote_ip'] + '/32'},
                        {"IpProtocol": "51", "FromPort": "-1", "ToPort": "-1", "CidrIp": vpn['remote_ip'] + '/32'},
                        {"IpProtocol": "udp", "FromPort": "500", "ToPort": "500", "CidrIp": vpn['remote_ip'] + '/32'},
                        {"IpProtocol": "udp", "FromPort": "4500", "ToPort": "4500", "CidrIp": vpn['remote_ip'] + '/32'},
                        {"IpProtocol": "tcp", "FromPort": "80", "ToPort": "80", "CidrIp": "0.0.0.0/0"},
                        {"IpProtocol": "tcp", "FromPort": "443", "ToPort": "443", "CidrIp": "0.0.0.0/0"},
                        {"IpProtocol": "udp", "FromPort": "123", "ToPort": "123", "CidrIp": "0.0.0.0/0"},
                        {"IpProtocol": "icmp", "FromPort": "-1", "ToPort": "-1", "CidrIp": "0.0.0.0/0"},
                        {"IpProtocol": "-1", "FromPort": "-1", "ToPort": "-1", "CidrIp": constants.SUPERNET}
                    ]
                )
            )
            _vpn_eip = self.add_resource(
                ec2.EIP(
                    self.cfn_name('VPNInstanceEIP', _vpn_name),
                    Domain='vpc'
                )
            )
            _vpn_eni = self.add_resource(
                ec2.NetworkInterface(
                    self.cfn_name('VPNInstanceENI', _vpn_name),
                    SubnetId=_vpn_subnet['SubnetId'],
                    Description='ENI for VPN - {}'.format(_vpn_name),
                    GroupSet=[Ref(_vpn_security_group)] + self.security_groups,
                    SourceDestCheck=False,
                    Tags=self.get_tags(role_override=_role)
                )
            )
            self.add_resource(
                ec2.EIPAssociation(
                    self.cfn_name('AssociateVPNInstanceENI', _vpn_name),
                    AllocationId=GetAtt(_vpn_eip, "AllocationId"),
                    NetworkInterfaceId=Ref(_vpn_eni)
                )
            )
            # Set up Routes from all VPC subnets to the ENI
            _vpc_route_tables = self.ec2_conn.describe_route_tables(
                Filters=[{'Name': 'vpc-id', 'Values': [self.vpc_id]}])['RouteTables']

            _local_subnets = iter(map(
                lambda x: constants.ENVIRONMENTS[x]['vpc']['cidrblock'],
                filter(lambda z: z in vpn.get('local_envs', []), constants.ENVIRONMENTS.keys())))
            _local_subnets = list(itertools.chain(_local_subnets, [self.vpc_metadata['cidrblock'], ]))

            # append remote vpc subnets
            _remote_subnets = iter(map(
                lambda x: constants.ENVIRONMENTS[x]['vpc']['cidrblock'],
                filter(lambda z: z in vpn.get('remote_envs', []), constants.ENVIRONMENTS.keys())))
            _remote_subnets = list(itertools.chain(_remote_subnets, vpn.get('remote_subnets', [])))

            for remote_subnet in _remote_subnets:
                for route_table in _vpc_route_tables:
                    self.add_resource(
                        ec2.Route(
                            self.cfn_name(_vpn_name, "VPNRoute", remote_subnet, route_table['RouteTableId']),
                            RouteTableId=route_table['RouteTableId'],
                            DestinationCidrBlock=remote_subnet,
                            NetworkInterfaceId=Ref(_vpn_eni)
                        )
                    )

            _user_data_template = self.get_cloudinit_template(
                replacements=(
                    ('__PROMPT_COLOR__', self.prompt_color()),
                    ('__LOCAL_SUBNETS__', ','.join(sorted(_local_subnets))),
                    ('__REMOTE_IP__', vpn['remote_ip']),
                    ('__REMOTE_SUBNETS__', ','.join(sorted(_remote_subnets))),
                    ('__SECRET__', vpn['secret']),
                    ('__IKE__', vpn.get('ike', 'aes256-sha1-modp1536')),
                    ('__IKE_LIFETIME__', vpn.get('ikelifetime', '28800s')),
                    ('__ESP__', vpn.get('esp', 'aes256-sha1')),
                    ('__KEYLIFE__', vpn.get('keylife', '1800s')),
                    ('__IPTABLES_RULES__', '\n'.join(vpn.get('iptables_rules', ''))),
                    ('__SERVICE__', self.service),
                    ('__VPN_NAME__', _vpn_name),
                    ('__TAG__', _vpn_name.lower()),
                    ('__VPC_ID__', self.vpc_id)
                )
            )
            _user_data = Sub(
                _user_data_template
                    .replace('${', '${!')  # Replace bash brackets with CFN escaped style
                    .replace('{#', '${'),  # Replace rain-style CFN escapes with proper CFN brackets,
                {
                    'CFN_EIP_ADDR': Ref(_vpn_eip),
                    'CFN_ENI_ID': Ref(_vpn_eni),
                }
            )

            _vpn_launch_configuration = self.add_resource(
                autoscaling.LaunchConfiguration(
                    self.cfn_name('VPNLaunchConfiguration', _vpn_name),
                    AssociatePublicIpAddress=True,
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
                    self.cfn_name('VPNASGroup', _vpn_name),
                    AvailabilityZones=[_vpn_subnet['AvailabilityZone']],
                    HealthCheckType='EC2',
                    LaunchConfigurationName=Ref(_vpn_launch_configuration),
                    MinSize=1,
                    MaxSize=1,
                    DesiredCapacity=1,
                    VPCZoneIdentifier=[_vpn_subnet['SubnetId']],
                    Tags=self.get_autoscaling_tags(role_override=_role) + [
                        autoscaling.Tag('Name', _role, True)
                    ]
                )
            )
