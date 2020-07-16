from troposphere import autoscaling, ec2, iam, Base64, GetAtt, Parameter, Ref

from config import constants
from .base import IvyTemplate
from utils.ec2 import get_block_device_mapping, get_latest_ami_id


class VPCTemplate(IvyTemplate):
    CAPABILITIES = ['CAPABILITY_IAM']

    def configure(self):
        self.vpc_metadata = constants.ENVIRONMENTS[self.env]['vpc']
        self.set_description('VPC, Routes, Base Security Groups, and NATs')

        common_vpc_tags = [
                              ec2.Tag('Name', self.env)
                          ] + self.get_tags(service_override='VPC')

        _vpc = self.add_resource(
            ec2.VPC(
                'VPC',
                CidrBlock=self.vpc_metadata['cidrblock'],
                EnableDnsSupport=True,
                EnableDnsHostnames=True,
                Tags=common_vpc_tags
            )
        )

        _dhcp_options = self.add_resource(
            ec2.DHCPOptions(
                'DHCPOptions',
                DomainName="node.{}.{}".format(self.env, constants.TAG),
                DomainNameServers=['AmazonProvidedDNS'],
                Tags=common_vpc_tags
            )
        )

        self.add_resource(
            ec2.VPCDHCPOptionsAssociation(
                'VPCDHCPOptionsAssociation',
                DhcpOptionsId=Ref(_dhcp_options),
                VpcId=Ref(_vpc)
            )
        )

        _internet_gateway = self.add_resource(
            ec2.InternetGateway(
                'InternetGateway',
                Tags=self.get_tags(service_override='InternetGateway', role_override='InternetGateway')
            )
        )
        self.add_resource(
            ec2.VPCGatewayAttachment(
                'AttachInternetGateway',
                VpcId=Ref(_vpc),
                InternetGatewayId=Ref(_internet_gateway)
            )
        )
        # route_tables stores all ec2.RouteTables generated and adds them to
        # a private vpc s3 endpoint
        route_tables = []
        _public_route_table = self.add_resource(
            ec2.RouteTable(
                'PublicRouteTable',
                VpcId=Ref(_vpc),
                Tags=self.get_tags(service_override='PublicRouteTable', role_override='PublicRouteTable')
            )
        )
        route_tables.append(_public_route_table)
        # Public Subnet Routes and ACLs
        self.add_resource(
            ec2.Route(
                'PublicRoute',
                RouteTableId=Ref(_public_route_table),
                DestinationCidrBlock='0.0.0.0/0',
                GatewayId=Ref(_internet_gateway)
            )
        )
        _public_network_acl = self.add_resource(
            ec2.NetworkAcl(
                'PublicNetworkAcl',
                VpcId=Ref(_vpc),
                Tags=self.get_tags(service_override='PublicNetworkAcl', role_override='PublicNetworkAcl')
            )
        )
        self.add_resource(
            ec2.NetworkAclEntry(
                'IngressPublicNetworkAclEntry',
                NetworkAclId=Ref(_public_network_acl),
                RuleNumber=100,
                Protocol='-1',
                RuleAction='allow',
                Egress=False,
                CidrBlock='0.0.0.0/0',
                PortRange=ec2.PortRange(From=1, To=65535)
            )
        )
        self.add_resource(
            ec2.NetworkAclEntry(
                'EgressPublicNetworkAclEntry',
                NetworkAclId=Ref(_public_network_acl),
                RuleNumber=101,
                Protocol='-1',
                RuleAction='allow',
                Egress=True,
                CidrBlock='0.0.0.0/0',
                PortRange=ec2.PortRange(From=1, To=65535)
            )
        )
        # Private Network ACLs
        _private_network_acl = self.add_resource(
            ec2.NetworkAcl(
                'PrivateNetworkAcl',
                VpcId=Ref(_vpc),
                Tags=self.get_tags(service_override='PrivateNetworkAcl', role_override='PrivateNetworkAcl')
            )
        )
        self.add_resource(
            ec2.NetworkAclEntry(
                'IngressPrivateNetworkAclEntry',
                NetworkAclId=Ref(_private_network_acl),
                RuleNumber=100,
                Protocol='-1',
                RuleAction='allow',
                Egress=False,
                CidrBlock='0.0.0.0/0',
                PortRange=ec2.PortRange(From=1, To=65535)
            )
        )
        self.add_resource(
            ec2.NetworkAclEntry(
                'EgressPrivateNetworkAclEntry',
                NetworkAclId=Ref(_private_network_acl),
                RuleNumber=101,
                Protocol='-1',
                RuleAction='allow',
                Egress=True,
                CidrBlock='0.0.0.0/0',
                PortRange=ec2.PortRange(From=1, To=65535)
            )
        )

        # Default security groups - referenced by name by constants/default-security-groups
        # _nat_security_group = self.add_resource(
        #     ec2.SecurityGroup(
        #         'NATSecurityGroup',
        #         VpcId=Ref(_vpc),
        #         GroupDescription='Security Group for NAT Instances',
        #         SecurityGroupIngress=[
        #             {'IpProtocol': '-1', 'FromPort': 1, 'ToPort': 65535, 'CidrIp': self.vpc_metadata['cidrblock']},
        #             {'IpProtocol': '-1', 'FromPort': 1, 'ToPort': 65535, 'CidrIp': '10.0.0.0/8'}
        #         ],
        #         Tags=self.get_tags(service_override='NAT', role_override='NAT-SecurityGroup')
        #     )
        # )
        # _consul_security_group = self.add_resource(
        #     ec2.SecurityGroup(
        #         'ConsulSecurityGroup',
        #         VpcId=Ref(_vpc),
        #         GroupDescription='Security Group for Consul access',
        #         SecurityGroupIngress=[
        #             {'IpProtocol': 'tcp', 'FromPort': 8300, 'ToPort': 8302, 'CidrIp': '10.0.0.0/8'},  # consul server rpc/serf
        #             {'IpProtocol': 'udp', 'FromPort': 8300, 'ToPort': 8302, 'CidrIp': '10.0.0.0/8'},  # consul server rpc/serf
        #             {'IpProtocol': 'tcp', 'FromPort': 8400, 'ToPort': 8400, 'CidrIp': '10.0.0.0/8'},  # consul client rpc
        #             {'IpProtocol': 'tcp', 'FromPort': 8500, 'ToPort': 8500, 'CidrIp': '10.0.0.0/8'},  # consul http
        #             {'IpProtocol': 'tcp', 'FromPort': 8600, 'ToPort': 8600, 'CidrIp': '10.0.0.0/8'},  # consul dns
        #             {'IpProtocol': 'udp', 'FromPort': 8600, 'ToPort': 8600, 'CidrIp': '10.0.0.0/8'}   # consul dns
        #         ],
        #         Tags=[
        #             ec2.Tag('ivy:team', self.TEAM['email']),
        #             ec2.Tag('ivy:environment', self.env),
        #             ec2.Tag('ivy:service', 'Consul'),
        #             ec2.Tag('ivy:role', 'Consul-SecurityGroup')
        #         ]
        #     )
        # )
        # _ssh_security_group = self.add_resource(
        #     ec2.SecurityGroup(
        #         'InternalSecurityGroup',
        #         VpcId=Ref(_vpc),
        #         GroupDescription='Internal Rules',
        #         SecurityGroupIngress=[
        #             {'IpProtocol': 'icmp', 'FromPort': -1, 'ToPort': -1, 'CidrIp': '10.0.0.0/8'},
        #             {'IpProtocol': 'tcp', 'FromPort': 22, 'ToPort': 22, 'CidrIp': '10.0.0.0/8'}
        #         ],
        #         SecurityGroupEgress=[
        #             {'IpProtocol': '-1', 'FromPort': 0, 'ToPort': 65535, 'CidrIp': '0.0.0.0/0'}
        #         ],
        #         Tags=[
        #             ec2.Tag('ivy:team', self.TEAM['email']),
        #             ec2.Tag('ivy:environment', self.env),
        #             ec2.Tag('ivy:service', 'infrastructure'),
        #             ec2.Tag('ivy:role', 'internal')
        #         ]
        #     )
        # )
        #
        # self.add_security_group(Ref(_nat_security_group), Ref(_consul_security_group), Ref(_ssh_security_group))

        ## This sets up all private and public AZs
        for index, zone in enumerate(self.vpc_metadata['zones'], 1):
            _public_subnet = self.add_resource(
                ec2.Subnet(
                    'PublicSubnet{}'.format(index),
                    VpcId=Ref(_vpc),
                    CidrBlock=zone['public-cidrblock'],
                    AvailabilityZone=zone['availability-zone'],
                    MapPublicIpOnLaunch=True,
                    Tags=self.get_tags(service_override='PublicSubnet', role_override='PublicSubnet{}'.format(index)) +
                         [
                             ec2.Tag('Name', '{}-PublicSubnet{}'.format(self.env, index))
                         ]
                )
            )
            self.add_resource(
                ec2.SubnetRouteTableAssociation(
                    'PublicSubnetRouteTableAssociation{}'.format(index),
                    SubnetId=Ref(_public_subnet),
                    RouteTableId=Ref(_public_route_table)
                )
            )
            self.add_resource(
                ec2.SubnetNetworkAclAssociation(
                    'PublicSubnetNetworkAclAssociation{}'.format(index),
                    SubnetId=Ref(_public_subnet),
                    NetworkAclId=Ref(_public_network_acl)
                )
            )

            # Allow VPCs with no private subnets (save money on NAT instances for VPCs with only public instances)
            if zone.get('private-cidrblock'):
                _private_subnet = self.add_resource(
                    ec2.Subnet(
                        'PrivateSubnet{}'.format(index),
                        VpcId=Ref(_vpc),
                        CidrBlock=zone['private-cidrblock'],
                        AvailabilityZone=zone['availability-zone'],
                        Tags=self.get_tags(service_override='PrivateSubnet',
                                           role_override='PrivateSubnet{}'.format(index)) +
                             [
                                 ec2.Tag('Name', '{}-PrivateSubnet{}'.format(self.env, index))
                             ]
                    )
                )
                # Private subnets get their own route table for AZ-specific NATs
                _private_route_table = self.add_resource(
                    ec2.RouteTable(
                        'PrivateRouteTable{}'.format(index),
                        VpcId=Ref(_vpc),
                        Tags=self.get_tags(service_override='PrivateRouteTable',
                                           role_override='PrivateRouteTable{}'.format(index))
                    )
                )
                route_tables.append(_private_route_table)

                # Create an EIP to be used with the NAT instance or gateway
                _nat_eip = self.add_resource(
                    ec2.EIP(
                        'NATInstanceEIP{}'.format(index),
                        Domain='vpc'
                    )
                )

                # Use VPC NAT Gateway
                _nat_gw = self.add_resource(
                    ec2.NatGateway(
                        'NATGateway{}'.format(index),
                        AllocationId=GetAtt(_nat_eip, "AllocationId"),
                        SubnetId=Ref(_public_subnet)
                    )
                )
                # Create a route via the NAT GW for the private route table
                self.add_resource(
                    ec2.Route(
                        'PrivateRoute{}'.format(index),
                        RouteTableId=Ref(_private_route_table),
                        DestinationCidrBlock='0.0.0.0/0',
                        NatGatewayId=Ref(_nat_gw)
                    )
                )

                self.add_resource(
                    ec2.SubnetRouteTableAssociation(
                        'PrivateSubnetRouteTableAssociation{}'.format(index),
                        SubnetId=Ref(_private_subnet),
                        RouteTableId=Ref(_private_route_table)
                    )
                )
                self.add_resource(
                    ec2.SubnetNetworkAclAssociation(
                        'PrivateSubnetNetworkAclAssociation{}'.format(index),
                        SubnetId=Ref(_private_subnet),
                        NetworkAclId=Ref(_private_network_acl)
                    )
                )

        # use route_table to create a VPC S3 endpoint
        self.add_resource(
            ec2.VPCEndpoint(
                'S3VPCEndpoint',
                RouteTableIds=[Ref(rt) for rt in route_tables],
                ServiceName='com.amazonaws.{}.s3'.format(self.region),
                VpcId=Ref(_vpc)
            )
        )
