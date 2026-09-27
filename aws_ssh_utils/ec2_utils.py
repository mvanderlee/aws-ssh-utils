from typing import TYPE_CHECKING

import questionary

from aws_ssh_utils.cli_utils import ShellError, spinner
from aws_ssh_utils.connection import Target, opkssh_provider

if TYPE_CHECKING:
    from mypy_boto3_ec2 import EC2Client
    from mypy_boto3_ec2.type_defs import InstanceTypeDef


def get_running_ec2_instances(ec2: "EC2Client") -> list["InstanceTypeDef"]:
    pages = ec2.get_paginator('describe_instances').paginate(
        Filters=[{'Name': 'instance-state-name', 'Values': ['running']}],
    )
    return [i for page in pages for r in page['Reservations'] for i in r.get('Instances', [])]


def ec2_name(instance: "InstanceTypeDef") -> str:
    """The Name tag, or the instance id when there is none."""
    return next((t.get('Value', '') for t in instance.get('Tags', []) if t.get('Key') == 'Name'), instance.get('InstanceId', ''))


def guess_ec2_user(ec2: "EC2Client", instance: "InstanceTypeDef") -> str:
    image_id = instance.get('ImageId')
    images = ec2.describe_images(ImageIds=[image_id]).get('Images', []) if image_id else []
    image_name = images[0].get('Name', '') if images else ''
    return 'ubuntu' if 'ubuntu' in image_name.lower() else 'ec2-user'


def ec2_target(ec2: "EC2Client", instance: "InstanceTypeDef", user: str | None = None) -> Target:
    """Connection target on the private IP. Guesses the user from the AMI name when not given."""
    tags = {t.get('Key', '').lower(): t.get('Value', '') for t in instance.get('Tags', [])}
    return Target(
        instance_id=instance.get('InstanceId', ''),
        ip=instance.get('PrivateIpAddress', ''),
        user=user or guess_ec2_user(ec2, instance),
        aliases=(ec2_name(instance),),
        key_name=instance.get('KeyName'),
        opkssh_provider=opkssh_provider(tags),
    )


def prompt_for_ec2_instance(
    ec2: "EC2Client",
    prompt: str = 'Which EC2 instance do you want to connect to?',
) -> "InstanceTypeDef":
    """Asks for a name filter, then which of the matching running instances to use."""
    with spinner():
        instances = get_running_ec2_instances(ec2)

    name_contains = questionary.text('Provide a name filter or leave blank to show all').unsafe_ask().lower()

    by_display_name: dict[str, InstanceTypeDef] = {}
    for instance in instances:
        name = ec2_name(instance)
        if name_contains and name_contains not in name.lower():
            continue
        # Disambiguate duplicate Name tags (e.g. ASG members) by appending the instance id
        display = name if name not in by_display_name else f'{name} ({instance.get("InstanceId")})'
        by_display_name[display] = instance

    if not by_display_name:
        raise ShellError('No matching EC2 instances found!')

    choice = questionary.select(prompt, choices=sorted(by_display_name)).unsafe_ask()
    return by_display_name[choice]
