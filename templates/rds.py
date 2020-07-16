from troposphere import ec2, rds, route53, GetAtt, Ref

from .base import IvyTemplate
from config import constants
import os


class RDSTemplate(IvyTemplate):

    def configure(self):
        rds_metadata = constants.ENVIRONMENTS[self.env]['rds']
        self.name = 'rds'
        self.add_description('Sets up an RDS Instance in a VPC')
        self.get_standard_parameters()
        self.get_default_security_groups()

        for db in rds_metadata:
            name = self.env + db['name']

            # get secrets
            env_name = "DB_{}_".format(db['name'])
            db_user = db.get('admin_user',
                             os.environ.get(env_name + "USER", None)
                             )
            db_pass = db.get('admin_pass',
                             os.environ.get(env_name + "PASS", None)
                             )

            if (db_user or db_pass) is None:
                raise KeyError("Database user or password not set. Please set {0}USER or {0}PASS environment variables"
                               .format(env_name))

            if db_user in ("rdsadmin", "admin"):
                raise ValueError("Database admin '{}' cannot be used as it is a reserved word used by the engine".format(db_user))

            tags = self.get_tags(service_override=self.name, role_override=db['name'])
            security_group = self.add_resource(
                ec2.SecurityGroup(
                    '{}RDSSecurityGroup'.format(name),
                    VpcId=self.vpc_id,
                    GroupDescription='Security Group for {} Access'.format(self.name),
                    SecurityGroupIngress=[
                        {'IpProtocol': 'tcp', 'FromPort': 5432, 'ToPort': 5432, 'CidrIp': self.vpc_cidr}  # Allow DB access
                    ],
                    Tags=tags
                )
            )
            self.add_security_group(Ref(security_group))

            # Default to true for preferred subnet unless using multi_az
            preferred_only = False if db.get('multi_az') is True else db.get('preferred_only', True)

            rds_subnet_group = self.add_resource(
                rds.DBSubnetGroup(
                    '{}RDSSubnetGroup'.format(name),
                    DBSubnetGroupDescription='Subnet group for {} RDS'.format(name),
                    SubnetIds=list(map(lambda x: x['SubnetId'], self.get_subnets('private', _preferred_only=preferred_only)))
                )
            )
            rds_parameter_group = self.add_resource(
                rds.DBParameterGroup(
                    '{}DBParameterGroup'.format(name),
                    Description='RDS ParameterGroup for {}'.format(name),
                    Family=db.get('engine_family', 'postgres11'),
                    Parameters={
                        'log_min_duration_statement': 250,
                        'max_connections': '{DBInstanceClassMemory/10485760}',
                        'pg_stat_statements.track': 'all',
                        'pg_stat_statements.max': db.get('max_logged_statements', '1000')
                    },
                    Tags=tags
                )
            )
            rds_instance = self.add_resource(
                rds.DBInstance(
                    '{}RDSInstance'.format(name),
                    AllocatedStorage=db['allocated_storage'],
                    AutoMinorVersionUpgrade=True,
                    BackupRetentionPeriod=7,
                    DBInstanceClass=db['instance_type'],
                    DBInstanceIdentifier=name,
                    DBParameterGroupName=Ref(rds_parameter_group),
                    #DBSnapshotIdentifier=db['snapshot_id'],
                    DBSubnetGroupName=Ref(rds_subnet_group),
                    Engine='postgres',
                    EngineVersion=db.get('engine_version', '11.5'),
                    LicenseModel='postgresql-license',
                    MultiAZ=db.get('multi_az', False),
                    PreferredBackupWindow='06:00-07:00',
                    PreferredMaintenanceWindow='sat:07:00-sat:08:00',
                    PubliclyAccessible=False,
                    StorageEncrypted=True,
                    StorageType='gp2',
                    Tags=tags,
                    VPCSecurityGroups=self.security_groups,
                    MasterUsername=db_user,
                    MasterUserPassword=db_pass,
                )
            )

            if self.get_partition() is 'aws': # aws-us-gov and aws-cn may not have route53 public zones
                hosted_zone = constants.ENVIRONMENTS[self.env]['route53_zone']
                self.add_resource(
                    route53.RecordSetGroup(
                        '{}Route53'.format(name),
                        HostedZoneName=hosted_zone,
                        RecordSets=[
                            route53.RecordSet(
                                Name='{}.rds.{}'.format(db['name'], hosted_zone),
                                ResourceRecords=[GetAtt(rds_instance, 'Endpoint.Address')],
                                Type='CNAME',
                                TTL=600
                            )
                        ]
                    )
                )
