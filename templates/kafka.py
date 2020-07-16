from troposphere import autoscaling, ec2, Base64, Parameter, Ref

from config import constants
from .base import IvyTemplate
from utils.ec2 import get_block_device_mapping, get_latest_ami_id, EBS_OPTIMIZED_INSTANCES


class KafkaTemplate(IvyTemplate):
    CAPABILITIES = ['CAPABILITY_IAM']

    def configure(self):
        """
        Returns a Kafka template
        """
        self.add_description('Configures Kafka in each AZ per config')
        self.service = 'kafka'
        self.get_default_security_groups()
        self.get_standard_parameters()
        self.get_standard_policies()

        _global_config = constants.ENVIRONMENTS[self.env]

        self.ami = self.add_parameter(
            Parameter(
                'AMI',
                Type='String',
                Description='AMI ID for instances',
                Default=get_latest_ami_id(self.region, "ivy-" + self.service, _global_config.get('ami_owner', 'self'))
            )
        )

        for cluster in constants.ENVIRONMENTS[self.env][self.service]:
            _cluster_name = "{}-{}".format(self.service, cluster['name'])  # {service}-app

            _security_group = self.add_resource(
                ec2.SecurityGroup(
                    self.cfn_name(_cluster_name, 'SecurityGroup'),
                    VpcId=self.vpc_id,
                    GroupDescription='Security Group for {} Instances'.format(self.service),
                    SecurityGroupIngress=[
                        {'IpProtocol': 'tcp', 'FromPort': 9091, 'ToPort': 9093, 'CidrIp': self.vpc_cidr},  # Kafka Standard
                        {'IpProtocol': 'tcp', 'FromPort': 9999, 'ToPort': 9999, 'CidrIp': self.vpc_cidr}   # JMX
                    ]
                )
            )
            self.add_resource(
                ec2.SecurityGroupIngress(
                    self.cfn_name(_cluster_name, 'IngressSecurityGroup'),
                    GroupId=Ref(_security_group),
                    IpProtocol='-1',
                    FromPort=-1,
                    ToPort=-1,
                    SourceSecurityGroupId=Ref(_security_group)  # this allows members all traffic
                )
            )
            self.add_security_group(Ref(_security_group))

            _block_device_mapping = get_block_device_mapping(self.parameters['InstanceType'].resource['Default'])
            _block_device_mapping += {
                ec2.BlockDeviceMapping(
                    DeviceName="/dev/xvda",
                    Ebs=ec2.EBSBlockDevice(
                        DeleteOnTermination=True,
                        VolumeSize=cluster.get('volume_size', 20),
                        VolumeType="gp2",
                    )
                )
            }

            _userdata = self.get_cloudinit_template(replacements=(
                ('__PROMPT_COLOR__', self.prompt_color()),
                ('__CLUSTER_NAME__', _cluster_name),
            ))

            _launch_configuration = self.add_resource(
                autoscaling.LaunchConfiguration(
                    self.cfn_name(_cluster_name, 'LaunchConfiguration'),
                    AssociatePublicIpAddress=False,
                    BlockDeviceMappings=_block_device_mapping,
                    ImageId=Ref(self.ami),
                    InstanceType=cluster.get('instance_type', 't2.nano'),
                    EbsOptimized=True if cluster.get('instance_type', 't2.nano') in EBS_OPTIMIZED_INSTANCES else False,
                    InstanceMonitoring=False,
                    IamInstanceProfile=Ref(self.instance_profile),
                    KeyName=Ref(self.keypair_name),
                    SecurityGroups=self.security_groups,
                    UserData=Base64(_userdata)
                )
            )
            self.add_resource(
                autoscaling.AutoScalingGroup(
                    self.cfn_name(_cluster_name, 'ASGroup'),
                    HealthCheckType='EC2',
                    LaunchConfigurationName=Ref(_launch_configuration),
                    MinSize=cluster.get('count', 3),
                    MaxSize=cluster.get('count', 3),
                    VPCZoneIdentifier=[subnet['SubnetId'] for subnet in self.get_subnets('private')],
                    Tags=self.get_autoscaling_tags(service_override=_cluster_name, role_override=self.service) + [
                        autoscaling.Tag('Name', "{}{}".format(self.env, _cluster_name), True)
                    ]
                )
            )

