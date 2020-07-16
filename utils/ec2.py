import boto3
from troposphere import ec2


INSTANCETYPE_TO_BLOCKDEVICEMAPPING = {
    'm3.medium': 1,
    'm3.large': 1,
    'm3.xlarge': 2,
    'm3.2xlarge': 2,
    'c3.large': 2,
    'c3.xlarge': 2,
    'c3.2xlarge': 2,
    'c3.4xlarge': 2,
    'c3.8xlarge': 2,
    'r3.large': 1,
    'r3.xlarge': 1,
    'r3.2xlarge': 1,
    'r3.4xlarge': 1,
    'r3.8xlarge': 2,
    'g2.2xlarge': 1,
    'g2.8xlarge': 2,
    'i2.xlarge': 1,
    'i2.2xlarge': 2,
    'i2.4xlarge': 4,
    'i2.8xlarge': 8,
    'd2.xlarge': 3,
    'd2.2xlarge': 6,
    'd2.4xlarge': 12,
    'd2.8xlarge': 24
}

EBS_OPTIMIZED_INSTANCES = [
    "t3.nano",
    "t3.micro",
    "t3.small",
    "t3.medium",
    "t3.large",
    "t3.xlarge",
    "t3.2xlarge",
    "c1.xlarge",
    "c3.xlarge",
    "c3.2xlarge",
    "c3.4xlarge",
    "c4.large",
    "c4.xlarge",
    "c4.2xlarge",
    "c4.4xlarge",
    "c4.8xlarge",
    "c5.4xlarge",
    "c5.24xlarge",
    "d2.xlarge",
    "d2.2xlarge",
    "d2.4xlarge",
    "d2.8xlarge",
    "g2.2xlarge",
    "i2.xlarge",
    "i2.2xlarge",
    "i2.4xlarge",
    "i3.large",
    "i3.xlarge",
    "i3.2xlarge",
    "i3.4xlarge",
    "i3.8xlarge",
    "i3.16xlarge",
    "m1.large",
    "m1.xlarge",
    "m2.2xlarge",
    "m2.4xlarge",
    "m3.xlarge",
    "m3.2xlarge",
    "m4.large",
    "m4.xlarge",
    "m4.2xlarge",
    "m4.4xlarge",
    "m4.10xlarge",
    "r3.xlarge",
    "r3.2xlarge",
    "r3.4xlarge",
    "r4.large",
    "r4.xlarge",
    "r4.2xlarge",
    "r4.4xlarge",
    "r4.8xlarge",
    "r4.16xlarge",
    "r5.2xlarge",
    "r5.4xlarge",
    "m5.large",
    "m5.xlarge",
    "m5.2xlarge",
    "m5.4xlarge",
    "m5.8xlarge",
    "m5.12xlarge",
    "m5.16xlarge",
    "m5.24xlarge",
]


def get_block_device_mapping(instanceType):
    mappings = []
    for i in range(INSTANCETYPE_TO_BLOCKDEVICEMAPPING.get(instanceType, 0)):
        mappings.append(
            ec2.BlockDeviceMapping(
                # this needs to wrap over to /dev/sdaa if we ever use d2.8xl instances
                DeviceName='/dev/sd{}'.format(chr(ord('m') + i)),
                VirtualName='ephemeral{}'.format(i)
            )
        )
    return mappings


def get_latest_ami_id(region, amiName, owner=None):
    ec2 = boto3.resource('ec2', region_name=region)
    images = ec2.images.filter(
        Filters=[{'Name': 'name', 'Values': ["{}*".format(amiName)]}],
        Owners=[owner if owner else 'self']
    )
    try:
        return sorted(images, key=lambda x: x.creation_date, reverse=True)[0].id
    except IndexError:
        raise IndexError('No AMIs match for name "{}"'.format(amiName))



def get_snapshots_by_tags(tags, latest=True, region='us-west-2'):
    """
    Returns boto3 snapshot objects ordered by newest first

    :param tags: (dict) {tag_name: tag_value}
    :param region: (string) AWS region
    :return: (list) boto3 snapshot objects ordered by start_time
    """
    ec2 = boto3.resource('ec2', region_name=region)
    snapshots = ec2.snapshots.filter(
        Filters=[{'Name': 'tag:{}'.format(k), 'Values': [v]} for k, v in tags.items()]
    )
    snapshots = sorted(snapshots, key=lambda x: x.start_time, reverse=True)
    if snapshots:
        return snapshots[0] if latest else snapshots
    else:
        return None
