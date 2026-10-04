from dataclasses import replace
from typing import Any

import boto3
import click
from loguru import logger

from aws_ssh_utils.cli_utils import (
    ShellError,
    detect_environment,
    handle_errors,
    open_shell,
    scrollback_option,
    spinner,
)
from aws_ssh_utils.ec2_utils import ec2_name, ec2_target, prompt_for_ec2_instance


@click.command('ec2')
@click.option('-p', '--profile', default=None, help='Which AWS profile to use')
@click.option('-r', '--region', default=None, help='Which AWS region to use')
@click.option('-u', '--user', default=None, help='Which user to connect as')
@click.option('--private/--public', default=True, help="Connect to the instance's private or public IP")
@click.option(
    '-k',
    '--key-file',
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Which key file to use to connect",
)
@scrollback_option
@handle_errors
def ec2(
    profile: str | None = None,
    region: str | None = None,
    user: str | None = None,
    private: bool = True,
    key_file: str | None = None,
    scrollback: int | None = None,
    **kwargs: Any,
):
    """
    Asks user which EC2 instance they want to connect to,
    then opens an interactive SSM or SSH session to the instance
    """
    session = boto3.Session(profile_name=profile, region_name=region)
    ec2_client = session.client('ec2')
    instance = prompt_for_ec2_instance(ec2_client)

    if user is None:
        logger.info('No user specified, attempting to detect required user...')
    with spinner():
        target = ec2_target(ec2_client, instance, user=user)

    ip = target.ip if private else instance.get('PublicIpAddress')
    if not ip:
        raise ShellError(f'{"Private" if private else "Public"} IP was requested, but none was found!')
    target = replace(target, ip=ip, key_file=key_file)

    open_shell(
        target,
        detect_environment(profile, session.region_name),
        title=f'{target.user}@{ec2_name(instance)}',
        scrollback=scrollback,
    )
