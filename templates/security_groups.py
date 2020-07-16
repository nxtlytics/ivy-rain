from troposphere import ec2, Ref, Output, Export
from config import constants
from .base import IvyTemplate


class SecurityGroupTemplate(IvyTemplate):
    CAPABILITIES = ['CAPABILITY_IAM']

    def configure(self):
        self.set_description('Shared and Default Security Groups')

        config = constants.ENVIRONMENTS[self.env]['security_groups']

        # Add default security groups
        for sg in config['default']:
            self._add_security_group(sg)

    def _add_security_group(self, sg_config):
        name = sg_config['name']
        description = sg_config['description']
        rules = sg_config['rules']

        # This is the name of the CFN output that will be referenced by other stacks
        output_name = self.default_sg_name(name)

        sg_resource = self.add_resource(
            ec2.SecurityGroup(
                self.cfn_name(name, 'SecurityGroup'),
                VpcId=self.vpc_id,
                GroupDescription=description,
                SecurityGroupIngress=rules,
                Tags=self.get_tags(service_override='SecurityGroup', role_override='{}-SecurityGroup'.format(name))
            )
        )

        self.add_output(
            Output(self.cfn_name(name, 'SecurityGroupExport'),
                   Export=Export(name=output_name),
                   Value=Ref(sg_resource))
        )
