import shlex
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import boto3
import click
import questionary
from loguru import logger

from aws_ssh_utils.cli_utils import ShellError, handle_errors, spinner
from aws_ssh_utils.connection import find_ssh_key_file, opkssh_login
from aws_ssh_utils.emr_utils import (
    EMR_USER,
    IP,
    get_emr_cluster_keys,
    get_emr_instance_ips,
    group_sort_key,
    prompt_for_emr_cluster,
)

if TYPE_CHECKING:
    from mypy_boto3_emr import EMRClient


@click.command('emr-all')
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
@handle_errors
def emr_all(
    profile: str | None = None,
    region: str | None = None,
    user: str | None = None,
    private: bool = True,
    key_file: str | None = None,
    **kwargs: Any,
):
    """
    Asks user which Cluster and EC2 instance they want to connect to,
    Then prints a tmux cli statement that will open a new session
    with a window per ec2 instance with ssh shell already opened.
    """
    session = boto3.Session(profile_name=profile, region_name=region)
    emr_client = session.client('emr')
    cluster_id, cluster_name = prompt_for_emr_cluster(emr_client)
    with spinner():
        grouped_instances = {k.split(" ")[0]: v for k, v in get_emr_instance_ips(emr_client, cluster_id).items()}

    user = user or EMR_USER
    if key_file is None:
        key_file = get_key_file(emr_client, cluster_id)

    def get_ip(ip: IP) -> str | None:
        if private:
            return ip.private
        if ip.public is None:
            raise ShellError(f'The instance with private IP {ip.private} does not have a public IP')
        return ip.public

    identity = f'-i {shlex.quote(key_file)} ' if key_file else ''
    window_cmds = [
        # "|| $SHELL -i" keeps the window open when ssh fails, so the error can be read.
        (f'{group_name} - {instance_num}', f'ssh {identity}{user}@{get_ip(instance_ip)} || $SHELL -i')
        for group_name, instance_ips in sorted(grouped_instances.items(), key=lambda item: group_sort_key(item[0]))
        for instance_num, instance_ip in enumerate(instance_ips, start=1)
    ]
    if not window_cmds:
        raise ShellError('No running instances found in the selected cluster!')

    first_window_name, first_cmd = window_cmds[0]
    tmux('new-session', '-d', '-s', cluster_name, '-n', first_window_name, first_cmd)
    for window_name, cmd in window_cmds[1:]:
        tmux('new-window', '-t', f'{cluster_name}:', '-n', window_name, cmd)
    tmux('switch-client', '-t', f'{cluster_name}:{first_window_name}')


def get_key_file(emr: "EMRClient", cluster_id: str) -> str | None:
    """The cluster's opkssh key (logging in when needed) or EC2 key, asking to continue when neither is found."""
    with spinner():
        key_name, provider = get_emr_cluster_keys(emr, cluster_id)
    if provider is not None:
        return opkssh_login(provider, on_output=logger.info, on_waiting=lambda: None)

    key_file = find_ssh_key_file(key_name)
    if (
        key_file is None
        and not questionary.confirm(
            f'Could not find the ssh key {key_name}, would you like to continue?',
        ).unsafe_ask()
    ):
        sys.exit(1)
    return key_file


def tmux(*args: str):
    subprocess.run(['tmux', *args], check=False)  # noqa: S603, S607
