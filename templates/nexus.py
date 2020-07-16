from troposphere import autoscaling, ec2, iam, route53, Base64, GetAtt, Parameter, Ref, Sub

from config import constants
from .base import IvyTemplate
from utils.ec2 import get_latest_ami_id


class NexusTemplate(IvyTemplate):
    CAPABILITIES = ['CAPABILITY_IAM']

    def configure(self):
        """
        Returns a Nexus template
        """
        self.defaults = {
            'instance_type': 't3.xlarge'
        }

        self.service = 'nexus'
        self.set_description('Sets up Nexus repository manager servers')
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

        # We want the preferred subnet only.
        subnet = self.get_subnets('private', _preferred_only=True)[0]

        # Add our security group
        security_group = self.add_resource(
            ec2.SecurityGroup(
                '{}SecurityGroup'.format(self.name),
                VpcId=self.vpc_id,
                GroupDescription='Security Group for {}'.format(self.name),
                SecurityGroupIngress=[
                    {"IpProtocol": "tcp", "FromPort": "80", "ToPort": "80", "CidrIp": constants.SUPERNET},  # HTTP
                    {"IpProtocol": "tcp", "FromPort": "443", "ToPort": "443", "CidrIp": constants.SUPERNET},  # HTTPS
                    # {"IpProtocol": "tcp", "FromPort": "8081", "ToPort": "8081", "CidrIp": constants.SUPERNET},  # NexusRM Direct (disabled!)
                ],
                SecurityGroupEgress=[
                    {"IpProtocol": "-1", "FromPort": "-1", "ToPort": "-1", "CidrIp": "0.0.0.0/0"}
                ]
            )
        )

        # Add our EBS data volume
        data_volume = ec2.Volume(
            '{}DataVolume'.format(self.name),
            Size=config.get('data_volume_size', 20),
            VolumeType='gp2',
            AvailabilityZone=subnet['AvailabilityZone'],
            DeletionPolicy='Retain',
            Tags=self.get_tags(service_override=self.service, role_override=self.name) + [ec2.Tag('Name', self.name + "-datavol")]
        )
        self.add_resource(data_volume)
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

        # Add a ENI for static IP address
        eni = self.add_resource(
            ec2.NetworkInterface(
                '{}InstanceENI'.format(self.name),
                SubnetId=subnet['SubnetId'],
                Description='ENI for {}'.format(self.name),
                GroupSet=[Ref(security_group)] + self.security_groups,
                SourceDestCheck=True,
                Tags=self.get_tags(service_override=self.service, role_override=self.name)
            )
        )
        self.get_eni_policies()

        # Add a route53 A record for the main Nexus host
        route53_zone = constants.ENVIRONMENTS[self.env]['route53_zone']
        private_dns = config.get('private_dns', 'nexus.{}'.format(route53_zone))
        self.add_resource(
            route53.RecordSetGroup(
                '{}Route53'.format(self.name),
                HostedZoneName=route53_zone,
                RecordSets=[
                    route53.RecordSet(
                        Name=private_dns,
                        ResourceRecords=[GetAtt(eni, 'PrimaryPrivateIpAddress')],
                        Type='A',
                        TTL=600
                    )
                ]
            )
        )
        # Add CNAME records for each repository, pointing to the main
        for repository in config['repositories']:
            self.add_resource(
                route53.RecordSetGroup(
                    '{}{}Route53'.format(self.name, self.cfn_name(repository)),
                    HostedZoneName=route53_zone,
                    RecordSets=[
                        route53.RecordSet(
                            Name='{}.{}'.format(repository, route53_zone),
                            ResourceRecords=[private_dns],
                            Type='CNAME',
                            TTL=600
                        )
                    ]
                )
            )

        # Add S3 IAM role for nexus blobstore access
        self.add_iam_policy(
            iam.Policy(
                PolicyName='S3Access',
                PolicyDocument={
                    'Statement': [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "s3:ListBucket",
                                "s3:GetBucketLocation",
                                "s3:ListBucketMultipartUploads",
                                "s3:ListBucketVersions",
                                "s3:GetBucketAcl",
                                "s3:GetLifecycleConfiguration",
                                "s3:PutLifecycleConfiguration"
                            ],
                            "Resource": [
                                'arn:{}:s3:::{}'.format(self.get_partition(), config['s3_bucket'])
                            ]
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "s3:GetObject",
                                "s3:PutObject",
                                "s3:DeleteObject",
                                "s3:AbortMultipartUpload",
                                "s3:ListMultipartUploadParts",
                                "s3:GetObjectTagging",
                                "s3:PutObjectTagging",
                                "s3:GetObjectTagging",
                                "s3:DeleteObjectTagging"
                            ],
                            "Resource": [
                                'arn:{}:s3:::{}/*'.format(self.get_partition(), config['s3_bucket'])
                            ]
                        }
                    ]
                }
            )
        )

        # Substitute the userdata template and feed it to CFN
        userdata_template = self.get_cloudinit_template(replacements=(
            ('__PROMPT_COLOR__', self.prompt_color()),
            ('__SERVICE__', self.service),
            ('__DEFAULT_DOMAIN__', route53_zone[:-1]),  # route53_zone has a trailing '.', strip it
            ('__TOP_DOMAIN__', constants.ROOT_ROUTE53_ZONE),
            # ('__REPOSITORIES__', " ".join(['"{}"'.format(x) for x in config['repositories']]))  # '"abc" "def" "ghi"'
        ))
        userdata = Sub(
            userdata_template
                .replace('${', '${!')  # Replace bash brackets with CFN escaped style
                .replace('{#', '${'),  # Replace rain-style CFN escapes with proper CFN brackets
            {
                'CFN_ENI_ID': Ref(eni),
                'CFN_EBS_ID': Ref(data_volume)
            }
        )

        launch_configuration = self.add_resource(
            autoscaling.LaunchConfiguration(
                '{}LaunchConfiguration'.format(self.name),
                AssociatePublicIpAddress=False,
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
                '{}ASGroup'.format(self.name),
                AvailabilityZones=[subnet['AvailabilityZone']],
                HealthCheckType='EC2',
                LaunchConfigurationName=Ref(launch_configuration),
                MinSize=0,
                MaxSize=1,
                DesiredCapacity=0,
                VPCZoneIdentifier=[subnet['SubnetId']],
                Tags=self.get_autoscaling_tags(service_override=self.service, role_override=self.name) + [
                    autoscaling.Tag('Name', self.name, True)
                ]
            )
        )
