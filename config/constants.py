TAG = 'ivy'

TEAMS = {
    'infrastructure': {
        'name': 'Infrastructure',
        'email': 'infeng@nxtlytics.com'
    }
}

SUPERNET = '10.0.0.0/8'
ROOT_ROUTE53_ZONE = 'nxtlytics.dev'

ENVIRONMENTS = {
    #
    # dev
    # Commercial development account for application code
    #
    'appdev': {
        'sysenv': 'ivy-aws-app-dev',
        'region': 'us-west-2',
        'prompt_color': 'green',
        'route53_zone': 'dev.nxtlytics.dev.',
        'vpc': {
            'cidrblock': '10.20.0.0/16',
            'zones': [
                {
                    'public-cidrblock': '10.20.0.0/20',
                    'private-cidrblock': '10.20.128.0/20',
                    'availability-zone': 'us-west-2a',
                    'preferred': True,
                },
                {
                    'public-cidrblock': '10.20.16.0/20',
                    'private-cidrblock': '10.20.144.0/20',
                    'availability-zone': 'us-west-2b',
                },
                {
                    'public-cidrblock': '10.20.32.0/20',
                    'private-cidrblock': '10.20.160.0/20',
                    'availability-zone': 'us-west-2c',
                }
            ]
        },
        'security_groups': {
            'default_unmanaged': [],
            'default': [
                {
                    'name': 'internal-all-ping',
                    'description': 'Allow ping to all instances',
                    'rules': [
                        {'IpProtocol': 'icmp', 'FromPort': -1, 'ToPort': -1, 'CidrIp': SUPERNET}
                    ]
                },
                {
                    'name': 'internal-consul',
                    'description': 'Allow consul to all instances',
                    'rules': [
                        # FIXME: Giant fixme here, this should not be SUPERNET!
                        {'IpProtocol': 'tcp', 'FromPort': 8301, 'ToPort': 8301, 'CidrIp': SUPERNET},
                        {'IpProtocol': 'udp', 'FromPort': 8301, 'ToPort': 8301, 'CidrIp': SUPERNET},
                    ]
                },
                {
                    'name': 'vpn-all-traffic',
                    'description': 'Allow all traffic from the VPN to all instances',
                    'rules': [
                        {'IpProtocol': -1, 'FromPort': -1, 'ToPort': -1, 'CidrIp': '10.255.0.0/16'}
                    ]
                }
            ],
            'shared': []
        },
        'mesos': {
            'master': {
                'instance_type': 't3.medium',
                'masters': [
                    '<Private IP>',
                    '<Private IP>',
                    '<Private IP>'
                ]
            },
            'agent': {
                'instance_type': 'c5.4xlarge',
                'private_elb_cert': 'dev.nxtlytics.dev',
                'public_elb_cert': 'dev.nxtlytics.dev',
                'rootfs_size': 20,
                'dockervol_size': 50,
                'preferred_placement': True,  # Place all instances in a single AZ to save inter-AZ bandwidth costs
                'count': {
                    'public': 0,
                    'private': 1
                },
                'iam_roles': [
                    {
                        'name': 'SampleBucket',
                        'buckets': ['ivy-samplebucket/*', ]
                    },
                ]

            }
        },
        'cassandra': {
            'clusters': [
                {
                    'name': 'app',
                    'cassandra_template': 'cassandra311',
                    'instance_type': 't3.xlarge',
                    'data_volume_size': 100,
                    'instances': [
                        # Template uses the first 3 for seeds
                        {'ip': '<Private IP>'},
                        {'ip': '<Private IP>'},
                        {'ip': '<Private IP>'},
                    ]
                }
            ]
        },
        'elasticache': [
            {
                'name': 'app',
                'engine': 'redis',
                'multi_az': True,
                'instance_type': 'cache.t3.small'
            }
        ],
        'kafka': [
            {
                'name': 'app',
                'instance_type': 't3.xlarge',
                'volume_size': 100,
                'count': 3
            }
        ],
        'rds': [
            {
                'name': 'app',
                'allocated_storage': 250,
                'instance_type': 'db.t3.large',
                'multi_az': True,
                'admin_user': 'ivyadmin',
                'engine_family': 'postgres11',
                'engine_version': '11.5',
            }
        ],
        'pritunl': {
            'public_dns': 'vpn.dev.nxtlytics.dev',
            'client_subnets': [
                '10.255.20.0/24'
            ],
            'mongodb': 'mongodb://vpn-internal..zone/pritunl',
            'server_id': '686deb4089466d3f44f60844a74fe47e'  # Random identifier, pre-created here so it stays static
        },
    },
    #
    # transit
    # Transit account/VPC for peering all Commercial accounts
    #
    'transit': {
        'sysenv': 'ivy-aws-transit-prod',
        'region': 'us-west-2',
        'prompt_color': 'red',
        'route53_zone': 'transit.nxtlytics.dev.',
        'vpc': {
            'cidrblock': '10.0.0.0/24',
            'zones': [
                {
                    'public-cidrblock': '10.0.0.0/26',
                    'availability-zone': 'us-west-2a',
                    'preferred': True,
                },
                {
                    'public-cidrblock': '10.0.0.64/26',
                    'availability-zone': 'us-west-2b',
                },
                {
                    'public-cidrblock': '10.0.0.128/26',
                    'availability-zone': 'us-west-2c',
                }
            ]
        },
        'security_groups': {
            'default_unmanaged': [],
            'default': [
                {
                    'name': 'internal-all-ping',
                    'description': 'Allow ping to all instances',
                    'rules': [
                        {'IpProtocol': 'icmp', 'FromPort': -1, 'ToPort': -1, 'CidrIp': SUPERNET}
                    ]
                }
            ],
            'shared': []
        },
        'vpn': [
            {
                # VPN for connecting AWS Commercial <-> AWS China
                # This VPN must include all subnets that will transit across the connection,
                # including 2nd degree connections.
                'active': True,
                'name': 'transit-cntransit',
                'remote_ip': '<Public IP>',  # IP of VPN in cntransit env
                # Use this for defining extra subnets that exist outside of CFN
                # 'remote_subnets': ['172.31.0.0/16'],
                # 'local_subnets': ['172.31.0.0/16'],
                # Define extra local envs here - this VPC is included by default
                'local_envs': ['dev',],
                'remote_envs': ['cntransit', 'cntools'],
            },
        ]
    },
}

PEERING = {
    # Source: Destination[s]. Peering will be established in the source VPC to the destinations.
    'aws-cn-tools': [
        {
            'peer': 'aws-tools',
            'type': 'remote'
        },
    ]
}

SSL_CERTIFICATES = {
    'dev.nxtlytics.dev': {
        "Type": "acm",
        "DomainName": "dev.nxtlytics.dev",
        "Arn": "arn:aws:acm:<region>:<account-id>:certificate/<uuid?>",
        "SubjectAlternativeNames": [
            "dev.nxtlytics.dev",
            "*.dev.nxtlytics.dev",
        ]
    },
    'cn-dev.nxtlytics.dev': {
        "Type": "acm",
        "DomainName": "cn-dev.nxtlytics.dev",
        "Arn": "arn:aws-cn:acm:<cn-region>:<account-id>:certificate/<uuid>",
        "SubjectAlternativeNames": [
            "cn-dev.nxtlytics.dev",
            "*.cn-dev.nxtlytics.dev",
        ]
    },

}
