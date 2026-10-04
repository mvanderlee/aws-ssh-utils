import sys
from dataclasses import replace
from typing import Any

import boto3
import click
import questionary

from aws_ssh_utils.cli_utils import (
    ShellError,
    detect_environment,
    handle_errors,
    open_shell,
    scrollback_option,
    spinner,
)
from aws_ssh_utils.connection import find_ssh_key_file
from aws_ssh_utils.emr_utils import emr_target, prompt_for_emr_cluster, prompt_for_emr_instance_group

CORE_NODE_INPUT = b'sudo su\r\ncd /var/log/hadoop-yarn/containers\r\n'


@click.command('emr')
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
def emr(
    profile: str | None = None,
    region: str | None = None,
    user: str | None = None,
    private: bool = True,
    key_file: str | None = None,
    scrollback: int | None = None,
    **kwargs: Any,
):
    """
    Asks user which Cluster and EC2 instance they want to connect to,
    then opens an interactive SSM or SSH session to the instance
    """
    session = boto3.Session(profile_name=profile, region_name=region)
    emr_client = session.client('emr')

    cluster_id, cluster_name = prompt_for_emr_cluster(emr_client)
    instance_ips, group_name = prompt_for_emr_instance_group(emr_client, cluster_id)
    instance_ips = sorted(instance_ips, key=lambda ip: ip.private or '')
    instance_options = [f'{ip.private} ({ip.public})' if ip.public else f'{ip.private}' for ip in instance_ips]
    choice = questionary.select('Which instance do you want to connect to?', choices=instance_options).unsafe_ask()
    group_idx = instance_options.index(choice)
    instance_ip = instance_ips[group_idx]

    ip = instance_ip.private if private else instance_ip.public
    if ip is None:
        raise ShellError(f'The selected instance does not have a {"private" if private else "public"} IP')

    with spinner():
        target = emr_target(emr_client, cluster_id, instance_ip.instance_id or '', ip)
    target = replace(target, user=user or target.user, key_file=key_file)
    env = detect_environment(profile, session.region_name)

    if (
        not env.ssm_installed
        and key_file is None
        and target.opkssh_provider is None
        and find_ssh_key_file(target.key_name) is None
        and not questionary.confirm(f'Could not find the ssh key {target.key_name}, would you like to continue?').unsafe_ask()
    ):
        sys.exit(1)

    group = group_name.split(' ')[0]
    postfix = f'[{group_idx}]' if group_idx else ''
    open_shell(
        target,
        env,
        title=f'{cluster_name} - {group}{postfix}',
        scrollback=scrollback,
        initial_input=CORE_NODE_INPUT if group.lower().startswith('core') else b'',
    )
