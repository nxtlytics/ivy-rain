from troposphere import Ref, ImportValue
from troposphere.ec2 import SecurityGroup

sg_name_matcher = {
    SecurityGroup: lambda sg: sg.title,
    Ref: lambda ref: ref.data['Ref'],
    ImportValue: lambda iv: iv.data['Fn::ImportValue']
}


def get_security_group_name(obj):
    """
    Get the name of a security group.
    Used to facilitate sorting lists of security groups.

    :param obj: SecurityGroup-like object (may be a ref, an imported value, or an actual security group)
    :return: Name of the security group
    """
    # poor man's pattern matcher
    return sg_name_matcher[type(obj)](obj).lower()
