from troposphere import (autoscaling, ec2, elasticloadbalancing, elasticloadbalancingv2,
                         cloudwatch, sns,iam, policies, route53, Base64, GetAtt, Parameter,
                         Ref)

from config import constants
from .base import IvyTemplate
from utils.ec2 import get_block_device_mapping, get_latest_ami_id, EBS_OPTIMIZED_INSTANCES


class MesosAgentsTemplate(IvyTemplate):
    elb_external_security_group = None

    def generate_load_balancer(self, lb_name, typ, port, cert_arn, log_bucket):

        lb_name = self.cfn_name(lb_name)

        if typ not in ['internal', 'internet-facing']:
            raise NameError("Load balancer type must be of type internal, internet-facing")

        # Use the system security groups (automatic) if internal, else use the limited external security group
        sg = self.security_groups if typ == 'internal' else [Ref(self.elb_external_security_group)]

        return elasticloadbalancing.LoadBalancer(
            lb_name,
            AccessLoggingPolicy=elasticloadbalancing.AccessLoggingPolicy(
                EmitInterval=60,
                Enabled=True,
                S3BucketName=log_bucket,
                S3BucketPrefix="ELB/{}/{}".format(self.env, lb_name)
            ),
            ConnectionDrainingPolicy=elasticloadbalancing.ConnectionDrainingPolicy(
                Enabled=True,
                Timeout=60
            ),
            ConnectionSettings=elasticloadbalancing.ConnectionSettings(
                IdleTimeout=3600
            ),
            CrossZone=False,
            HealthCheck=elasticloadbalancing.HealthCheck(
                HealthyThreshold=5,
                Interval=30,
                Target='HTTP:{}/ping'.format(port),
                Timeout=5,
                UnhealthyThreshold=2
            ),
            LoadBalancerName=lb_name,
            Listeners=[
                elasticloadbalancing.Listener(
                    InstancePort=port,
                    InstanceProtocol='HTTP',
                    LoadBalancerPort=80,
                    Protocol='HTTP'
                ),
                elasticloadbalancing.Listener(
                    InstancePort=port,
                    InstanceProtocol='HTTP',
                    LoadBalancerPort=443,
                    Protocol='HTTPS',
                    SSLCertificateId=cert_arn
                ),
                elasticloadbalancing.Listener(
                    InstancePort=port,
                    InstanceProtocol='TCP',
                    LoadBalancerPort=8443,
                    Protocol='SSL',
                    SSLCertificateId=cert_arn
                )
            ],
            Policies=[
                elasticloadbalancing.Policy(
                    PolicyName='ELBSecurityPolicyNoTLS10',
                    PolicyType='SSLNegotiationPolicyType',
                    Attributes=[{
                        'Name': 'Reference-Security-Policy',
                        # Disable TLS 1.0 and migrate to TLS 1.2 default for external ELB
                        'Value': 'ELBSecurityPolicy-TLS-1-2-2017-01'
                    }]
                )
            ],
            Scheme=typ,
            SecurityGroups=sg,
            Subnets=[s['SubnetId'] for s in self.get_subnets('private' if typ == 'internal' else 'public')],
            Tags=self.get_tags(
                service_override="InternalELB" if typ == 'internal' else "ExternalELB",
                role_override=lb_name
            ) + [ec2.Tag('Name', lb_name)]
        )

    def generate_app_load_balancer(self, lb_name, typ, port, cert_arn, log_bucket):

        lb_name = self.cfn_name(lb_name)

        if typ not in ['internal', 'internet-facing']:
            raise NameError("Load balancer type must be of type internal, internet-facing")

        # Use the system security groups (automatic) if internal, else use the limited external security group
        sg = self.security_groups if typ == 'internal' else [Ref(self.elb_external_security_group)]

        _alb = elasticloadbalancingv2.LoadBalancer(
            lb_name,
            Name=lb_name,
            IpAddressType='ipv4',
            LoadBalancerAttributes=[
                elasticloadbalancingv2.LoadBalancerAttributes(
                    Key='access_logs.s3.enabled',
                    Value='true'
                ),
                elasticloadbalancingv2.LoadBalancerAttributes(
                    Key='access_logs.s3.bucket',
                    Value=log_bucket
                ),
                elasticloadbalancingv2.LoadBalancerAttributes(
                    Key='access_logs.s3.prefix',
                    Value="ELB/{}/{}".format(self.env, lb_name)
                ),
                elasticloadbalancingv2.LoadBalancerAttributes(
                    Key='deletion_protection.enabled',
                    Value='false'
                ),
                elasticloadbalancingv2.LoadBalancerAttributes(
                    Key='idle_timeout.timeout_seconds',
                    Value='60'
                ),
                elasticloadbalancingv2.LoadBalancerAttributes(
                    Key='routing.http.drop_invalid_header_fields.enabled',
                    Value='false'
                ),
                elasticloadbalancingv2.LoadBalancerAttributes(
                    Key='routing.http2.enabled',
                    Value='true'
                )
            ],
            Scheme=typ,
            SecurityGroups=sg,
            Subnets=[s['SubnetId'] for s in self.get_subnets('private' if typ == 'internal' else 'public')],
            Type='application',
            Tags=self.get_tags(
                service_override="InternalALB" if typ == 'internal' else "ExternalALB",
                role_override=lb_name
            ) + [ec2.Tag('Name', lb_name)]
        )

        _target_group = elasticloadbalancingv2.TargetGroup(
            '{}TG'.format(lb_name),
            Name='{}TG'.format(lb_name)[0:31],
            HealthCheckIntervalSeconds=30,
            HealthCheckPath='/ping',
            HealthCheckPort=port,
            HealthCheckProtocol='HTTP',
            HealthCheckTimeoutSeconds=5,
            HealthyThresholdCount=5,
            UnhealthyThresholdCount=2,
            Matcher=elasticloadbalancingv2.Matcher(
                HttpCode='200'
            ),
            Port=port,
            Protocol='HTTP',
            TargetGroupAttributes=[
                elasticloadbalancingv2.TargetGroupAttribute(
                    Key='deregistration_delay.timeout_seconds',
                    Value='300'
                ),
                elasticloadbalancingv2.TargetGroupAttribute(
                    Key='stickiness.enabled',
                    Value='false'
                ),
                elasticloadbalancingv2.TargetGroupAttribute(
                    Key='stickiness.type',
                    Value='lb_cookie'
                ),
                elasticloadbalancingv2.TargetGroupAttribute(
                    Key='load_balancing.algorithm.type',
                    Value='least_outstanding_requests'
                )
            ],
            TargetType='instance',
            VpcId=self.vpc_id,
            Tags=self.get_tags(
                service_override="InternalALB" if typ == 'internal' else "ExternalALB",
                role_override=lb_name
            ) + [ec2.Tag('Name', '{}TG'.format(lb_name))]
        )

        _listener_80 = self.add_resource(elasticloadbalancingv2.Listener(
            '{}80Listener'.format(lb_name),
            Port='80',
            Protocol='HTTP',
            LoadBalancerArn=Ref(_alb),
            DefaultActions=[
                elasticloadbalancingv2.Action(
                    Type='redirect',
                    RedirectConfig=elasticloadbalancingv2.RedirectConfig(
                        Host='#{host}',
                        Path='/#{path}',
                        Port='443',
                        Protocol='HTTPS',
                        Query='#{query}',
                        StatusCode='HTTP_301'
                    )
                )
            ],
        ))
        _listener_443 = self.add_resource(elasticloadbalancingv2.Listener(
            '{}443Listener'.format(lb_name),
            Port='443',
            Protocol='HTTPS',
            LoadBalancerArn=Ref(_alb),
            SslPolicy='ELBSecurityPolicy-2016-08',
            Certificates=[
                elasticloadbalancingv2.Certificate(
                    CertificateArn=cert_arn
                )
            ],
            DefaultActions=[
                elasticloadbalancingv2.Action(
                    Type='forward',
                    TargetGroupArn=Ref(_target_group)
                )
            ],
        ))
        return _alb, _target_group

    def generate_asg(self, placement, count, block_mapping, load_balancers=None, target_group_arns=None, preferred_subnets_only=False):
        if placement not in ["public", "private"]:
            raise NameError("Mesos ASG must be either public or private")

        mesos_masters = constants.ENVIRONMENTS[self.env]['mesos']['master']['masters']
        user_data = self.get_cloudinit_template(
            replacements=(
                ('__PROMPT_COLOR__', self.prompt_color()),
                ('__PLACEMENT__', placement),
                ('__ZK_CONNECT__', ','.join(['{}:2181'.format(z) for z in mesos_masters]))
            )
        )

        # Datadog webhook for scaling events
        # sns_topic = self.add_resource(
        #     sns.Topic(
        #         "MesosASG",
        #         Subscription=[
        #             sns.Subscription(
        #                 Endpoint='https://app.datadoghq.com/intake/webhook/sns?api_key=',
        #                 Protocol='https'
        #             )
        #         ]
        #     )
        # )

        role_name = "Mesos{}Agent".format(placement.capitalize())

        launch_configuration = self.add_resource(
            autoscaling.LaunchConfiguration(
                '{}LaunchConfiguration'.format(role_name),
                AssociatePublicIpAddress=False,
                BlockDeviceMappings=block_mapping,
                EbsOptimized=True if self.defaults.get('instance_type') in EBS_OPTIMIZED_INSTANCES else False,
                KeyName=Ref(self.keypair_name),
                ImageId=Ref(self.ami),
                IamInstanceProfile=Ref(self.instance_profile),
                InstanceType=Ref(self.instance_type),
                InstanceMonitoring=False,
                SecurityGroups=self.security_groups,
                UserData=Base64(user_data)
            )
        )

        self.add_resource(
            autoscaling.AutoScalingGroup(
                '{}ASGroup'.format(role_name),
                AvailabilityZones=[subnet['AvailabilityZone'] for subnet in
                                   self.get_subnets(placement, _preferred_only=preferred_subnets_only)],
                HealthCheckType='ELB',
                HealthCheckGracePeriod=600,
                LaunchConfigurationName=Ref(launch_configuration),
                LoadBalancerNames=load_balancers if target_group_arns == None else [],
                TargetGroupARNs=target_group_arns if load_balancers == None else [],
                MinSize=count,
                MaxSize=100,
                VPCZoneIdentifier=[subnet['SubnetId'] for subnet in
                                   self.get_subnets(placement, _preferred_only=preferred_subnets_only)],
                Tags=self.get_autoscaling_tags(service_override="MesosAgent",
                                               role_override=role_name) + [
                         autoscaling.Tag('Name', self.env + role_name, True)
                     ],
                # NotificationConfigurations=[
                #     autoscaling.NotificationConfigurations(
                #         TopicARN=Ref(_sns_topic),
                #         NotificationTypes=[
                #             autoscaling.EC2_INSTANCE_LAUNCH,
                #             autoscaling.EC2_INSTANCE_LAUNCH_ERROR,
                #             autoscaling.EC2_INSTANCE_TERMINATE,
                #             autoscaling.EC2_INSTANCE_TERMINATE_ERROR
                #         ]
                #     )
                # ]
            )
        )

    def configure(self):
        config = constants.ENVIRONMENTS[self.env]['mesos']['agent']
        self.defaults = {
            'instance_type': config.get('instance_type', 'r5.xlarge')
        }

        self.add_description('Sets up Mesos Agents in all Zones')
        self.get_standard_parameters()
        self.get_standard_policies()
        self.get_default_security_groups()

        _global_config = constants.ENVIRONMENTS[self.env]

        self.ami = self.add_parameter(
            Parameter(
                'AMI',
                Type='String',
                Description='AMI ID for instances',
                Default=get_latest_ami_id(self.region, 'ivy-mesos', _global_config.get('ami_owner', 'self'))
            )
        )

        # Mesos Agent Security Group
        self.mesos_agent_security_group = self.add_resource(
            ec2.SecurityGroup(
                'MesosAgentSecurityGroup',
                VpcId=self.vpc_id,
                GroupDescription='Security Group for MesosAgent Instances',
                SecurityGroupIngress=[
                    # public http via ELB
                    {'IpProtocol': 'tcp', 'FromPort': 80, 'ToPort': 80, 'CidrIp': self.vpc_cidr},
                    # internal service SSL direct
                    {'IpProtocol': 'tcp', 'FromPort': 443, 'ToPort': 443, 'CidrIp': self.vpc_cidr},
                    # host-network services (tcp)
                    {'IpProtocol': 'tcp', 'FromPort': 5000, 'ToPort': 5049, 'CidrIp': self.vpc_cidr},
                    # host-network services (udp)
                    {'IpProtocol': 'udp', 'FromPort': 5000, 'ToPort': 5049, 'CidrIp': self.vpc_cidr},
                    # mesos agent api
                    {'IpProtocol': 'tcp', 'FromPort': 5050, 'ToPort': 5051, 'CidrIp': self.vpc_cidr},
                    # internal http-alt direct
                    {'IpProtocol': 'tcp', 'FromPort': 8000, 'ToPort': 8000, 'CidrIp': self.vpc_cidr},
                    # internal http via ELB
                    {'IpProtocol': 'tcp', 'FromPort': 8080, 'ToPort': 8080, 'CidrIp': self.vpc_cidr},
                    # internal http-alt direct
                    {'IpProtocol': 'tcp', 'FromPort': 9090, 'ToPort': 9090, 'CidrIp': self.vpc_cidr},
                    # mesos tasks (udp)
                    {'IpProtocol': 'udp', 'FromPort': 31000, 'ToPort': 32000, 'CidrIp': self.vpc_cidr},
                    # mesos tasks (tcp)
                    {'IpProtocol': 'tcp', 'FromPort': 31000, 'ToPort': 32000, 'CidrIp': self.vpc_cidr}
                ]
            )
        )
        self.add_resource(
            ec2.SecurityGroupIngress(
                'MesosAgentIngressSecurityGroup',
                GroupId=Ref(self.mesos_agent_security_group),
                IpProtocol='-1',
                FromPort=-1,
                ToPort=-1,
                SourceSecurityGroupId=Ref(self.mesos_agent_security_group)
                # All Mesos agents can access all ports on each other
            )
        )
        self.add_security_group(Ref(self.mesos_agent_security_group))

        # Security group for the internet-facing (external) ELBs - not added to the mesos agents themselves
        self.elb_external_security_group = self.add_resource(
            ec2.SecurityGroup(
                'MesosAgentELBExternalSecurityGroup',
                VpcId=self.vpc_id,
                GroupDescription='External Security Group for MesosAgent ELB Instances',
                SecurityGroupIngress=[
                    {'IpProtocol': 'tcp', 'FromPort': 80, 'ToPort': 80, 'CidrIp': '0.0.0.0/0'},  # http
                    {'IpProtocol': 'tcp', 'FromPort': 443, 'ToPort': 443, 'CidrIp': '0.0.0.0/0'},  # https
                    {'IpProtocol': 'tcp', 'FromPort': 8443, 'ToPort': 8443, 'CidrIp': '0.0.0.0/0'},  # https-alt
                    {'IpProtocol': 'icmp', 'FromPort': -1, 'ToPort': -1, 'CidrIp': '0.0.0.0/0'}  # ping (health checks)
                ]
            )
        )

        #
        # Docker roles
        #

        # Allow assume /docker roles by ec2metaproxy
        self.add_iam_policy(
            iam.Policy(
                PolicyName='AssumeDockerRoles',
                PolicyDocument={
                    'Statement': [
                        {
                            'Effect': 'Allow',
                            'Action': ["sts:AssumeRole"],
                            "Resource": {
                                "Fn::Join": [
                                    "",
                                    ["arn:{}:iam::".format(self.get_partition()), {"Ref": "AWS::AccountId"}, ":role/docker/*"]
                                ]
                            },
                        }
                    ]
                }
            )
        )
        # Add docker roles to assumable roles list
        for r in self.generate_docker_roles():
            self.add_resource(r)

        #
        # Load Balancers
        #

        lb_type = config.get('lb_type', 'classic')
        elb_log_bucket = config.get('log_bucket', '{}-{}-logs'.format(constants.TAG, self.env))

        if lb_type == 'classic':
            internal_elb = self.add_resource(
                self.generate_load_balancer(
                    "{}MesosAgentInternalELB".format(self.env),
                    "internal",
                    8080,
                    constants.SSL_CERTIFICATES[config['private_elb_cert']]['Arn'],
                    elb_log_bucket
                )
            )

            external_elb = self.add_resource(
                self.generate_load_balancer(
                    "{}MesosAgentExternalELB".format(self.env),
                    "internet-facing",
                    80,
                    constants.SSL_CERTIFICATES[config['public_elb_cert']]['Arn'],
                    elb_log_bucket
                )
            )
        elif lb_type == 'application':
            internal_elb, internal_target_group = self.generate_app_load_balancer(
                "{}MesosAgentInternalALB".format(self.env),
                "internal",
                8080,
                constants.SSL_CERTIFICATES[config['private_elb_cert']]['Arn'],
                elb_log_bucket
            )
            self.add_resource(internal_elb)
            self.add_resource(internal_target_group)

            external_elb, external_target_group = self.generate_app_load_balancer(
                "{}MesosAgentExternalALB".format(self.env),
                "internet-facing",
                80,
                constants.SSL_CERTIFICATES[config['public_elb_cert']]['Arn'],
                elb_log_bucket
            )
            self.add_resource(external_elb)
            self.add_resource(external_target_group)

        # extra public load balancers (for SSL termination, ELB doesn't do SNI)
        extra_public_load_balancers = []
        for lb_config in config.get('extra_public_load_balancers', []):
            if lb_type == 'classic':
                extra_public_load_balancers.append(Ref(self.add_resource(
                    self.generate_load_balancer(
                        "{}{}MesosAgentExternalELB".format(self.env, lb_config['name']),
                        "internet-facing",
                        80,
                        constants.SSL_CERTIFICATES[lb_config['cert']]['Arn'],
                        elb_log_bucket
                    )
                )))
            elif lb_type == 'application':
                _extra_public_lb, _extra_external_tg = self.generate_app_load_balancer(
                    "{}{}MesosAgentExternalALB".format(self.env, lb_config['name']),
                    "internet-facing",
                    80,
                    constants.SSL_CERTIFICATES[lb_config['cert']]['Arn'],
                    elb_log_bucket
                )
                self.add_resource(_extra_public_lb)
                extra_public_load_balancers.append(Ref(self.add_resource(_extra_external_tg)))

        #
        # Instances
        #

        # Add docker volume
        block_device_mapping = get_block_device_mapping(self.parameters['InstanceType'].resource['Default'])
        block_device_mapping.extend([
            ec2.BlockDeviceMapping(
                DeviceName="/dev/xvda",  # rootfs
                Ebs=ec2.EBSBlockDevice(
                    DeleteOnTermination=True,
                    VolumeSize=config.get('rootfs_size', 50),
                    VolumeType="gp2"
                )
            ),
            ec2.BlockDeviceMapping(
                DeviceName="/dev/xvdb",
                Ebs=ec2.EBSBlockDevice(
                    DeleteOnTermination=True,
                    VolumeSize=config.get('dockervol_size', 100),
                    VolumeType=config.get('dockervol_type', 'gp2')
                )
            )
        ])

        # Launch configurations
        preferred_only = config.get('preferred_placement', False)

        if lb_type == 'classic':
            # Private ASG
            self.generate_asg("private",
                              count=config['count'].get('private', 2),
                              block_mapping=block_device_mapping,
                              load_balancers=[Ref(internal_elb), Ref(external_elb)] + extra_public_load_balancers,
                              preferred_subnets_only=preferred_only
                              )

            # Public ASG
            self.generate_asg("public",
                              count=config['count'].get('public', 0),
                              block_mapping=block_device_mapping,
                              load_balancers=[Ref(internal_elb), Ref(external_elb)] + extra_public_load_balancers,
                              preferred_subnets_only=preferred_only
                              )
        elif lb_type == 'application':
            # Private ASG
            self.generate_asg("private",
                              count=config['count'].get('private', 2),
                              block_mapping=block_device_mapping,
                              target_group_arns=[Ref(internal_target_group), Ref(external_target_group)] + extra_public_load_balancers,
                              preferred_subnets_only=preferred_only
                              )

            # Public ASG
            self.generate_asg("public",
                              count=config['count'].get('public', 0),
                              block_mapping=block_device_mapping,
                              target_group_arns=[Ref(internal_target_group), Ref(external_target_group)] + extra_public_load_balancers,
                              preferred_subnets_only=preferred_only
                              )

        #
        # DNS Records
        #

        if self.get_partition() != 'aws-us-gov':
            zone = constants.ENVIRONMENTS[self.env]['route53_zone']
            self.add_resource(
                route53.RecordSetGroup(
                    'ELBRoute53',
                    HostedZoneName=zone,
                    RecordSets=[
                        route53.RecordSet(
                            Name='internal.{}'.format(zone)[:-1],
                            ResourceRecords=[GetAtt(internal_elb, 'DNSName')],
                            Type='CNAME',
                            TTL=300
                        ),
                        route53.RecordSet(
                            Name='external.{}'.format(zone)[:-1],
                            ResourceRecords=[GetAtt(external_elb, 'DNSName')],
                            Type='CNAME',
                            TTL=300
                        )
                    ]
                )
            )
