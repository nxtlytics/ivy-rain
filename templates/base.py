import boto3
import logging
import os
import re

from troposphere import ec2, iam, Parameter, Ref, Template, ImportValue, Sub, Join, autoscaling, GetAtt
from awacs import ec2 as iam_ec2
from awacs import aws as iam_aws
from utils import security_groups

from config import constants

logger = logging.getLogger(__name__)


class IvyTemplate(Template):
    ENVIRONMENT = None
    TEAM = constants.TEAMS['infrastructure']
    CAPABILITIES = ['CAPABILITY_IAM']

    def __init__(self, template_name, env, params):
        super(IvyTemplate, self).__init__()
        self.env = env
        self.params = params
        self.defaults = {}
        self.region = constants.ENVIRONMENTS[self.env]['region']
        self.sysenv = constants.ENVIRONMENTS[self.env]['sysenv']
        self.ec2_conn = boto3.client('ec2', region_name=self.region)
        self.name = self.env + template_name
        self.template_name = template_name
        self.tpl_name = template_name.lower()
        self._security_groups = set()
        self.instance_role = None
        self.instance_profile = None
        self.configure()

    @property
    def security_groups(self):
        """
        Troposhere expect a list, so we store a set internally but expose a list.
        Sort it to prevent changing templates unnecessarily.
        TODO: Sorting security groups may introduce rule ordering issues at a later date
        :return: list
        """
        return sorted(list(self._security_groups), key=security_groups.get_security_group_name)

    @property
    def vpc_id(self):
        """
        Get the VPC ID for the current template.
        Allows override of the VPC ID if necessary.
        """
        vpc_override = constants.ENVIRONMENTS[self.env]['vpc'].get('vpc_id')
        return vpc_override or self.get_vpc()['VpcId']

    def configure(self):
        # This must be overridden by subclasses
        raise NotImplementedError

    def get_standard_parameters(self):
        """
        Injects into template commonly used parameters
        """
        self.vpc_metadata = constants.ENVIRONMENTS[self.env]['vpc']
        self.vpc_cidr = self.vpc_metadata['cidrblock']
        self.keypair_name = self.add_parameter(
            Parameter(
                'KeyPairName',
                Type='String',
                Description='Name of EC2 Keypair to use for instances',
                Default=self.params.get('KeyPairName', self.defaults.get('keypair_name', 'ansible'))
            )
        )
        self.instance_type = self.add_parameter(
            Parameter(
                'InstanceType',
                Type='String',
                Description='EC2 Instance type',
                Default=self.params.get('InstanceType', self.defaults.get('instance_type', 't2.nano'))
            )
        )

    def get_tags(self, service_override=None, role_override=None, typ=None):
        """
        Get the default tags for this environment
        :return:
        """
        return [
            ec2.Tag('{}:environment'.format(constants.TAG), self.env),
            ec2.Tag('{}:sysenv'.format(constants.TAG), self.sysenv),
            ec2.Tag('{}:service'.format(constants.TAG), service_override if service_override else self.template_name),
            ec2.Tag('{}:role'.format(constants.TAG), role_override if role_override else self.name),
            ec2.Tag('{}:team'.format(constants.TAG), self.TEAM['email']),
        ]

    def get_autoscaling_tags(self, service_override=None, role_override=None):
        """
        Get the default autoscaling tags for this environment
        :return:
        """
        return [
            autoscaling.Tag('{}:environment'.format(constants.TAG), self.env, True),
            autoscaling.Tag('{}:sysenv'.format(constants.TAG), self.sysenv, True),
            autoscaling.Tag('{}:service'.format(constants.TAG), service_override if service_override else self.template_name, True),
            autoscaling.Tag('{}:role'.format(constants.TAG), role_override if role_override else self.name, True),
            autoscaling.Tag('{}:team'.format(constants.TAG), self.TEAM['email'], True),
        ]

    def add_iam_policy(self, policy):
        if not hasattr(self, 'policies'):
            self.policies = []
        if not isinstance(policy, iam.Policy):
            raise RuntimeError('Policy must be a troposhere iam.Policy object')
        self.policies.append(policy)
        self.instance_role = iam.Role(
            '{}InstanceRole'.format(self.name),
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
            Policies=self.policies
        )
        self.instance_profile = iam.InstanceProfile(
            '{}InstanceProfile'.format(self.name),
            Path='/',
            Roles=[Ref(self.instance_role)]
        )
        self.resources[self.instance_role.title] = self.instance_role
        self.resources[self.instance_profile.title] = self.instance_profile

    def add_security_group(self, *security_groups):
        for sg in security_groups:
            self._security_groups.add(sg)

    def get_standard_policies(self):
        self.add_iam_policy(
            iam.Policy(
                PolicyName='DescribePermissions',
                PolicyDocument={
                    'Statement': [{
                        'Effect': 'Allow',
                        'Action': [
                            'ec2:DescribeDhcpOptions',
                            'ec2:DescribeInstances',
                            'ec2:DescribeNetworkInterfaces',
                            'ec2:DescribeRegions',
                            'ec2:DescribeVpcs'
                        ],
                        'Resource': '*'
                    }]
                }
            )
        )

    def get_eni_policies(self):
        if not (self.instance_role or self.instance_profile):
            self.get_standard_policies()

        # iam_aws.PolicyDocument(
        #     Statement=[iam_aws.Statement(
        #         Effect=iam_aws.Allow,
        #         Action=[
        #             iam_ec2.AttachNetworkInterface,
        #             iam_ec2.DetachNetworkInterface
        #         ],
        #         Resource=[
        #             Sub('arn:${AWS::Partition}:ec2:${AWS::Region}:${AWS::AccountId}:network-interface/*')
        #         ],
        #     )]
        # )

        self.add_iam_policy(
            iam.Policy(
                PolicyName='ManageENI',
                PolicyDocument={
                    'Statement': [{
                        'Effect': 'Allow',
                        'Action': [
                            'ec2:AttachNetworkInterface',
                            'ec2:DetachNetworkInterface'
                        ],
                        'Resource': '*'
                    }]
                }
            )
        )

    def _generate_policy_statements(self, role):
        statements = []
        if role.get('buckets'):
            statements.extend(
                [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "s3:ListBucket"
                        ],
                        "Resource": list({"arn:{}:s3:::{}".format(self.get_partition(), b.split('/')[0]) for b in role['buckets']})
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "s3:Put*",
                            "s3:Get*",
                            "s3:DeleteObject"
                        ],
                        "Resource": ["arn:{}:s3:::{}".format(self.get_partition(), b) for b in role['buckets']]
                    }
                ]
            )
        statements.extend(role.get('other', []))
        return statements

    def generate_docker_roles(self):
        """
        Generate assumable roles for ec2metaproxy docker containers
        """
        roles = [
            iam.Role(
                r['name'],
                AssumeRolePolicyDocument={
                    'Statement': [{
                        'Effect': 'Allow',
                        'Principal': {
                            "AWS": [GetAtt(self.instance_role, 'Arn')]
                        },
                        'Action': ['sts:AssumeRole']
                    }]
                },
                Path='/docker/',
                Policies=[
                    iam.Policy(
                        PolicyName='{}Policy'.format(r['name']),
                        PolicyDocument={
                            'Statement': self._generate_policy_statements(r)
                        }
                    )
                ]
            ) for r in constants.ENVIRONMENTS[self.env]['mesos']['agent']['iam_roles']
        ]

        return roles

    def get_partition(self):
        return self.ec2_conn.meta.partition

    def default_sg_name(self, name):
        return '{}-{}-DefaultSecurityGroup'.format(self.env, name)

    def get_default_security_groups(self):
        """
        This function will set self._security_groups to include a pre-configured list of
        SGs to use based on tags in contants.py

        """
        # Add managed security groups using
        for sg in constants.ENVIRONMENTS[self.env]['security_groups']['default']:
            self.add_security_group(ImportValue(self.default_sg_name(sg['name'])))

        # Add unmanaged (created outside CloudFormation) security groups to instances
        _unmanaged_filters = [
            {'Name': 'vpc-id', 'Values': [self.vpc_id]},
            {'Name': 'tag:{}:environment'.format(constants.TAG), 'Values': [self.env]}
        ]
        for sg_filters in constants.ENVIRONMENTS[self.env]['security_groups'].get('default_unmanaged'):
            for sg in self.ec2_conn.describe_security_groups(Filters=_unmanaged_filters + sg_filters).get(
                    'SecurityGroups'):
                self.add_security_group(sg['GroupId'])

    def _search_tags(self, tags, key, value):
        """
        For a given key, searches through a list of tag dicts to return if exists substring matches on the tag value.
        Search is case insensitive.
        :param tags: (list) of {"Key":<string>, "Value":<string>} tags
        :param key: (string) tag key to filter
        :param value: (string) value for substring search
        :return: (dict) matching Tag dict
        """
        for t in tags:
            if key == t.get('Key', None):
                v = t.get('Value', None)
                if v and v.lower().find(value.lower()) > -1:
                    return t
        return None

    def get_vpc(self):
        """
        Returns a VPC based on self.env
        :return: (dict) representing a VPC
        """
        result = self.ec2_conn.describe_vpcs(Filters=[
            {'Name': 'tag:{}:service'.format(constants.TAG), 'Values': ['VPC']},
            {'Name': 'tag:{}:environment'.format(constants.TAG), 'Values': [self.env]}
        ])['Vpcs']
        if len(result) == 0:
            raise Exception('VPC {} not found in region {}'.format(self.env, self.region))
        elif len(result) > 1:
            raise Exception('More than 1 VPC {} found in region {}'.format(self.env, self.region))
        return result[0]

    def get_subnets(self, _filter=None, _preferred_only=False):
        """
        Returns subnets for the current environment/vpc
        :param _filter: (string) can be None, 'private', or 'public'
        :param _preferred_only: (bool) return preferred subnets only?
        :return: (list) representing subnets
        """
        if _filter not in [None, 'private', 'public']:
            raise RuntimeError('Filter not one of None, "public", or "private": {}'.format(_filter))
        filter_is_public = True if _filter == 'public' else False
        all_subnets = self.ec2_conn.describe_subnets(
            Filters=[{'Name': 'vpc-id', 'Values': [self.vpc_id]}])['Subnets']
        if _filter:
            subnets = filter(lambda s: s['MapPublicIpOnLaunch'] == filter_is_public, all_subnets)
        else:
            # set subnets to the filtered version, use this for any additional filters
            subnets = all_subnets

        if not _preferred_only:
            return list(subnets)
        else:
            preferred_availability_zones = list(map(lambda x: x.get('availability-zone'),
                                                    filter(lambda x: x.get('preferred', False),
                                                           constants.ENVIRONMENTS[self.env]['vpc']['zones'])))
            return list(filter(lambda x: (x.get('AvailabilityZone') in preferred_availability_zones), subnets))

    def get_route_tables(self, _filter=None):
        """
        Returns route_tables for the current environment/vpc.
        :param _filter: (string) can be None, 'private', or 'public'
        :return: (list) representing route_tables
        """
        if _filter not in [None, 'private', 'public']:
            raise RuntimeError('Filter not one of None, "public", or "private": {}'.format(_filter))
        route_tables = self.ec2_conn.describe_route_tables(
            Filters=[{'Name': 'vpc-id', 'Values': [self.get_vpc()['VpcId']]}]
        )['RouteTables']
        if _filter:
            return filter(lambda rt: self._search_tags(rt['Tags'], '{}:role'.format(constants.TAG), _filter),
                          route_tables)
        else:
            return route_tables

    def get_cloudinit_template(self, _tpl_name=None, replacements=None):
        """
        Returns the cloudinit data from a file in instance-data/templatename.tpl
        :return: (string) instance data
        """
        if not _tpl_name:
            _tpl_name = self.tpl_name
        with open("{}.sh.tpl".format(os.path.join("instance-data", _tpl_name))) as f:
            template = f.read()
        if replacements is not None:
            for replacement in replacements:
                if not isinstance(replacement, tuple):
                    raise ValueError('Replacements must be a tuple of tuples to replace')
                from_string = str(replacement[0])
                to_string = str(replacement[1])
                if len(replacement) == 3:
                    max = replacement[2]
                    template = template.replace(from_string, to_string, max)
                else:
                    template = template.replace(from_string, to_string)
        return template

    def cfn_name(self, *args):
        """
        Concat strings together in a Cloudformation safe manner. Also strips characters that aren't allowed from
        the final result

        :param args: Strings to be concatenated together and stripped
        :return: Final Cloudformation safe string
        """
        out = ""
        for arg in args:
            out += arg

        return re.sub("[^a-zA-Z0-9]", "", out)

    def prompt_color(self):
        return constants.ENVIRONMENTS[self.env].get('prompt_color', 'green')
