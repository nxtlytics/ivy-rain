from troposphere import autoscaling, ec2, iam, route53, Base64, Parameter, Ref, Sub, GetAtt

from config import constants
from .base import IvyTemplate
from utils.ec2 import get_latest_ami_id
import textwrap


class BindTemplate(IvyTemplate):
    CAPABILITIES = ['CAPABILITY_IAM']

    def make_bind_zone(self, zone):
        """ Creates an individual zone for a given zone config """

        upstreams = '; '.join(zone['upstreams']) + '; '  # '127.0.0.1; 192.168.1.1; '
        return textwrap.dedent("""\
            zone "{zone}." IN {{
               type forward;
               forward only;
               forwarders {{ {upstreams} }};
            }};
        """.format(zone=zone['name'], upstreams=upstreams))

    def configure(self):
        """
        Returns a BIND template
        """
        self.defaults = {
            'instance_type': 't3.micro'
        }

        self.service = 'bind'
        self.set_description('Sets up BIND DNS servers')
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

        config = constants.ENVIRONMENTS[self.env][self.service]

        # All subnets in public get a DNS server
        subnets = self.get_subnets('public')

        # Add our security group
        security_group = self.add_resource(
            ec2.SecurityGroup(
                '{}SecurityGroup'.format(self.name),
                VpcId=self.vpc_id,
                GroupDescription='Security Group for {}'.format(self.name),
                SecurityGroupIngress=[
                    {"IpProtocol": "tcp", "FromPort": "53", "ToPort": "53", "CidrIp": "0.0.0.0/0"},  # DNS TCP
                    {"IpProtocol": "udp", "FromPort": "53", "ToPort": "53", "CidrIp": "0.0.0.0/0"},  # DNS UDP
                ],
                SecurityGroupEgress=[
                    {"IpProtocol": "-1", "FromPort": "-1", "ToPort": "-1", "CidrIp": "0.0.0.0/0"}
                ]
            )
        )

        route53_zone = constants.ENVIRONMENTS[self.env]['route53_zone']

        zonefile = ''
        for zone in config['forwarders']:
            zonefile += "\n" + self.make_bind_zone(zone)

        for subnet in subnets:
            subnet_name = subnet['AvailabilityZone']
            role = '{}-{}-{}'.format(self.env, self.service, subnet_name)  # myenv-bind-us-west-2a

            # Add the elastic IP and the ENI for it, then attach it.
            eip = self.add_resource(
                ec2.EIP(
                    '{}InstanceEIP'.format(self.cfn_name(role)),
                    Domain='vpc'
                )
            )
            eni = self.add_resource(
                ec2.NetworkInterface(
                    '{}InstanceENI'.format(self.cfn_name(role)),
                    SubnetId=subnet['SubnetId'],
                    Description='ENI for {}'.format(role),
                    GroupSet=[Ref(security_group)] + self.security_groups,
                    SourceDestCheck=True,
                    Tags=self.get_tags(service_override=self.service, role_override=role)
                )
            )
            self.get_eni_policies()

            self.add_resource(
                ec2.EIPAssociation(
                    '{}AssociateVPNInstanceENI'.format(self.cfn_name(role)),
                    AllocationId=GetAtt(eip, "AllocationId"),
                    NetworkInterfaceId=Ref(eni)
                )
            )

            # Add a route53 DNS name
            self.add_resource(
                route53.RecordSetGroup(
                    '{}Route53'.format(self.cfn_name(role)),
                    HostedZoneName=route53_zone,
                    RecordSets=[
                        route53.RecordSet(
                            Name="{}.{}".format(role, route53_zone),
                            ResourceRecords=[Ref(eip)],
                            Type='A',
                            TTL=600
                        )
                    ]
                )
            )

            # Substitute the userdata template and feed it to CFN
            userdata_template = self.get_cloudinit_template(replacements=(
                ('__PROMPT_COLOR__', self.prompt_color()),
                ('__SERVICE__', self.service),
                ('__BIND_ZONEFILE__', zonefile)
            ))
            userdata = Sub(
                userdata_template
                    .replace('${', '${!')  # Replace bash brackets with CFN escaped style
                    .replace('{#', '${'),  # Replace rain-style CFN escapes with proper CFN brackets
                {
                    'CFN_ENI_ID': Ref(eni)
                }
            )

            launch_configuration = self.add_resource(
                autoscaling.LaunchConfiguration(
                    '{}LaunchConfiguration'.format(self.cfn_name(role)),
                    AssociatePublicIpAddress=True,
                    KeyName=Ref(self.keypair_name),
                    ImageId=Ref(self.ami),
                    InstanceType=Ref(self.instance_type),
                    InstanceMonitoring=False,
                    IamInstanceProfile=Ref(self.instance_profile),
                    UserData=Base64(userdata)
                )
            )
            self.add_resource(
                autoscaling.AutoScalingGroup(
                    '{}ASGroup'.format(self.cfn_name(role)),
                    AvailabilityZones=[subnet['AvailabilityZone']],
                    HealthCheckType='EC2',
                    LaunchConfigurationName=Ref(launch_configuration),
                    MinSize=0,
                    MaxSize=1,
                    DesiredCapacity=0,
                    VPCZoneIdentifier=[subnet['SubnetId']],
                    Tags=self.get_autoscaling_tags(service_override=self.service, role_override=role) + [
                        autoscaling.Tag('Name', role, True)
                    ]
                )
            )
