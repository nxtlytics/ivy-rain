from troposphere import autoscaling, ec2, iam, kms, secretsmanager, Base64, GetAtt, Parameter, Ref, Sub

import netaddr
from config import constants
from .base import IvyTemplate
from utils.ec2 import get_block_device_mapping, get_latest_ami_id


class VaultTemplate(IvyTemplate):
    CAPABILITIES = ['CAPABILITY_IAM']

    def configure(self):
        """
        This template creates a vault and consul master per subnet in the VPC
        """
        config = constants.ENVIRONMENTS[self.env]['vault']
        self.defaults = {
            'instance_type': config.get('instance_type', 't3.large')
        }

        self.set_description('Sets up Vault and Consul Masters in all Zones')
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
                Default=get_latest_ami_id(self.region, 'ivy-vault', _global_config.get('ami_owner', 'self'))
            )
        )
        _vault_security_group = self.add_resource(
            ec2.SecurityGroup(
                'VaultSecurityGroup',
                VpcId=self.vpc_id,
                GroupDescription='Security Group for Vault Instances',
                SecurityGroupIngress=[
                    {'IpProtocol': 'tcp', 'FromPort': 8200, 'ToPort': 8201, 'CidrIp': self.vpc_cidr},  # vault rpc/lan serf
                    {'IpProtocol': 'udp', 'FromPort': 8200, 'ToPort': 8201, 'CidrIp': self.vpc_cidr},  # vault rpc/lan serf (udp)
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
                'VaultIngressSecurityGroup',
                GroupId=Ref(_vault_security_group),
                IpProtocol='-1',
                FromPort=-1,
                ToPort=-1,
                SourceSecurityGroupId=Ref(_vault_security_group)
                # this allows members all traffic (for replication)
            )
        )
        self.add_security_group(Ref(_vault_security_group))

        _vault_kms_key = kms.Key(
            'VaultKMSUnseal',
            Description='Vault unseal key',
            PendingWindowInDays=10,
            KeyPolicy={
                'Version': '2012-10-17',
                'Id': 'key-default-1',
                'Statement': [
                    {
                        'Sid': 'Enable IAM User Permissions',
                        'Effect': 'Allow',
                        'Principal': {
                            'AWS': Sub('arn:${AWS::Partition}:iam::${AWS::AccountId}:root')
                        },
                        'Action': 'kms:*',
                        'Resource': '*'
                    },
                    {
                        'Sid': 'Allow administration of the key',
                        'Effect': 'Allow',
                        'Principal': {
                            'AWS': Sub('arn:${{AWS::Partition}}:iam::${{AWS::AccountId}}:role/${{{0}InstanceRole}}'.format(self.name))
                        },
                        'Action': [
                            'kms:Create*',
                            'kms:Describe*',
                            'kms:Enable*',
                            'kms:List*',
                            'kms:Put*',
                            'kms:Update*',
                            'kms:Revoke*',
                            'kms:Disable*',
                            'kms:Get*',
                            'kms:Delete*',
                            'kms:ScheduleKeyDeletion',
                            'kms:CancelKeyDeletion'
                        ],
                        'Resource': '*'
                    },
                    {
                        'Sid': 'Allow use of the key',
                        'Effect': 'Allow',
                        'Principal': {
                            'AWS': Sub('arn:${{AWS::Partition}}:iam::${{AWS::AccountId}}:role/${{{0}InstanceRole}}'.format(self.name))
                        },
                        'Action': [
                            'kms:DescribeKey',
                            'kms:Encrypt',
                            'kms:Decrypt',
                            'kms:ReEncrypt*',
                            'kms:GenerateDataKey',
                            'kms:GenerateDataKeyWithoutPlaintext'
                        ],
                        'Resource': '*'
                    }
                ]
            },
            Tags=self.get_tags(
                service_override="Vault"
            ) + [ec2.Tag('Name', 'VaultKMSUnseal')]
        )

        self.add_resource(_vault_kms_key)

        _vault_secretsmanager_secret = secretsmanager.Secret(
            'VaultSecret{}'.format(self.env),
            Description='Vault Root/Recovery key',
            Name='VaultSecret-{}'.format(self.env),
            KmsKeyId=Ref(_vault_kms_key),
            Tags=self.get_tags(
                service_override="Vault"
            ) + [ec2.Tag('Name', 'VaultSecret-{}'.format(self.env))]
        )

        self.add_resource(_vault_secretsmanager_secret)

        # Add support for creating/updating secretsmanager entries
        # You may need more permissions if use a customer-managed AWS KMS key to encrypt the secret.
        # - kms:GenerateDataKey
        # - kms:Decrypt
        self.add_iam_policy(iam.Policy(
            PolicyName='VaultSecretsManagerAccess',
            PolicyDocument={
                'Statement': [
                    {
                        'Effect': 'Allow',
                        'Resource': Sub('arn:${{AWS::Partition}}:secretsmanager:${{AWS::Region}}:${{AWS::AccountId}}:secret:VaultSecret-{0}*'.format(self.env)),
                        'Action': [
                            'secretsmanager:UpdateSecretVersionStage',
                            'secretsmanager:UpdateSecret',
                            'secretsmanager:PutSecretValue',
                            'secretsmanager:GetSecretValue',
                            'secretsmanager:DescribeSecret',
                            'secretsmanager:TagResource'
                        ]
                    },
                    {
                        'Effect': 'Allow',
                        'Resource': '*',
                        'Action': [
                            'iam:GetRole'
                        ]
                    }
                ]
            }
        ))

        _vault_client_role = iam.Role(
            '{}ClientRole'.format(self.name),
            AssumeRolePolicyDocument={
                'Statement': [{
                    'Effect': 'Allow',
                    'Principal': {
                        'Service': ['ec2.amazonaws.com']
                    },
                    'Action': ['sts:AssumeRole']
                }]
            },
            Path='/',
            Policies=[iam.Policy(
                PolicyName='VaultClientPolicy',
                PolicyDocument={
                    'Statement': [
                        {
                            'Effect': 'Allow',
                            'Resource': '*',
                            'Action': [
                                'ec2:DescribeInstances',
                                'iam:GetInstanceProfile',
                                'iam:GetUser',
                                'iam:GetRole'
                            ]
                        }
                    ]
                }
            )]
        )

        self.add_resource(_vault_client_role)

        masters = [(index, ip) for index, ip in enumerate(config['masters'], 1)]
        subnets = self.get_subnets('private')
        for master in masters:
            zone_index, master_ip = master
            subnet = [s for s in subnets if netaddr.IPAddress(master_ip) in netaddr.IPNetwork(s['CidrBlock'])][0]

            _vault_eni = ec2.NetworkInterface(
                'VaultInstanceENI{}'.format(subnet['AvailabilityZone'][-1]),
                Description='ENI for Vault ENV: {0}  PrivateSubnet {1}'.format(self.env, subnet['SubnetId']),
                GroupSet=self.security_groups,
                PrivateIpAddress=master_ip,
                SourceDestCheck=True,
                SubnetId=subnet['SubnetId'],
                Tags=self.get_tags(service_override="Vault",
                                   role_override='Vault-{}'.format(subnet['AvailabilityZone']))
            )
            self.add_resource(_vault_eni)

            _user_data_template = self.get_cloudinit_template(
                replacements=(
                    ('__PROMPT_COLOR__', self.prompt_color()),
                    ('__IVY_TAG__', constants.TAG),
                    ('__ENI_IP__', master_ip),
                    ('__SERVER_ID__', zone_index),
                    ('__VAULT_SECRET__', 'VaultSecret-{}'.format(self.env)),
                    ('__HOSTS_ENTRIES__', '\n'.join(
                        ['{0} vault-master-{1}.node.{2}.{3} vault-master-{1}'.
                             format(ip, index, self.env, constants.TAG) for index, ip in masters]
                    ))
                )
            )

            _user_data = Sub(
                _user_data_template
                    .replace('${', '${!')  # Replace bash brackets with CFN escaped style
                    .replace('{#', '${'),  # Replace rain-style CFN escapes with proper CFN brackets
                {
                    'CFN_ENI_ID': Ref(_vault_eni),
                    'VAULT_CLIENT_ROLE_NAME': Ref(_vault_client_role),
                    'VAULT_CLIENT_ROLE': GetAtt(_vault_client_role, 'Arn'),
                }
            )

            _vault_launch_configuration = self.add_resource(
                autoscaling.LaunchConfiguration(
                    'VaultLaunchConfiguration{}'.format(subnet['AvailabilityZone'][-1]),
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
                    'VaultASGroup{}'.format(subnet['AvailabilityZone'][-1]),
                    AvailabilityZones=[subnet['AvailabilityZone']],
                    HealthCheckType='EC2',
                    LaunchConfigurationName=Ref(_vault_launch_configuration),
                    MinSize=0,
                    MaxSize=1,
                    VPCZoneIdentifier=[subnet['SubnetId']],
                    Tags=self.get_autoscaling_tags(
                        service_override="Vault",
                        role_override='Vault-{}'.format(subnet['AvailabilityZone'])) + [
                             autoscaling.Tag('Name', '{}Vault-{}'.format(self.env, subnet['AvailabilityZone']),
                                             True)
                         ]
                )
            )
