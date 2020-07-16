from troposphere import ec2, elasticache, rds, route53, GetAtt, Ref

from .base import IvyTemplate
from config import constants


class ElastiCacheTemplate(IvyTemplate):

    def configure(self):
        elasticache_metadata = constants.ENVIRONMENTS[self.env]['elasticache']
        self.name = 'elasticache'
        self.add_description('Sets up elasticache in VPC')
        self.get_standard_parameters()
        self.get_default_security_groups()

        for cache in elasticache_metadata:
            name = self.env + cache['name'].replace('-', '').capitalize() + cache['engine'].capitalize()
            tags = self.get_tags(service_override=self.name, role_override=cache['name'])
            _port = 6379 if cache['engine'] == 'redis' else 11211
            security_group = self.add_resource(
                ec2.SecurityGroup(
                    '{}ElastiCacheSecurityGroup'.format(name),
                    VpcId=self.vpc_id,
                    GroupDescription='Security Group for {} Access'.format(name),
                    SecurityGroupIngress=[
                        {'IpProtocol': 'tcp', 'FromPort': _port, 'ToPort': _port, 'CidrIp': self.vpc_cidr}
                    ],
                    Tags=tags
                )
            )
            # Default to true for preferred subnet unless using multi_az
            preferred_only = False if cache.get('multi_az') is True else cache.get('preferred_only', True)
            subnet_group = self.add_resource(
                elasticache.SubnetGroup(
                    '{}SubnetGroup'.format(name),
                    Description='SubnetGroup for {} Elasticache'.format(name),
                    SubnetIds=list(map(lambda x: x['SubnetId'], self.get_subnets('private', _preferred_only=preferred_only)))
                )
            )
            if cache['engine'] == 'redis':
                cache_cluster = self.add_resource(
                    elasticache.ReplicationGroup(
                        '{}ReplicationGroup'.format(name),
                        AutomaticFailoverEnabled=False,
                        AutoMinorVersionUpgrade=True,
                        CacheNodeType=cache['instance_type'],
                        CacheSubnetGroupName=Ref(subnet_group),
                        Engine='redis',
                        EngineVersion=cache.get('engine_version', '5.0.6'),
                        NumCacheClusters=1,
                        ReplicationGroupDescription='{} RedisElasticache Cluster'.format(name),
                        SecurityGroupIds=[Ref(security_group)]
                    )
                )
                records = [GetAtt(cache_cluster, 'PrimaryEndPoint.Address')]
            else:
                cache_cluster = self.add_resource(
                    elasticache.CacheCluster(
                        '{}CacheCluster'.format(name),
                        AutoMinorVersionUpgrade=True,
                        CacheNodeType=cache['instance_type'],
                        CacheSubnetGroupName=Ref(subnet_group),
                        ClusterName=cache.get('cluster_name', name),
                        Engine='memcached',
                        EngineVersion='1.5.16',
                        NumCacheNodes=3,
                        VpcSecurityGroupIds=[Ref(security_group)]
                    )
                )
                records = [GetAtt(cache_cluster, 'ConfigurationEndpoint.Address')]

            if self.get_partition() != 'aws-us-gov':
                hosted_zone = constants.ENVIRONMENTS[self.env]['route53_zone']
                self.add_resource(
                    route53.RecordSetGroup(
                        '{}Route53'.format(name),
                        HostedZoneName=hosted_zone,
                        RecordSets=[
                            route53.RecordSet(
                                Name='{}.{}.{}'.format(cache['name'], cache['engine'], hosted_zone),
                                ResourceRecords=[GetAtt(cache_cluster, 'PrimaryEndPoint.Address')],
                                Type='CNAME',
                                TTL=600
                            )
                        ]
                    )
                )

