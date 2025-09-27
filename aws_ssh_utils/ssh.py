import datetime as dt
import logging
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any

import boto3
import click
import click_spinner
import paramiko
import paramiko.client
import questionary
from botocore.exceptions import ClientError
from environs import Env
from loguru import logger
from typing_extensions import override

from .emr_utils import (
    IP,
    get_emr_instance_ips,
    prompt_for_emr_cluster,
    prompt_for_emr_instance_group,
)
from .interactive_ssh import interactive_shell

if TYPE_CHECKING:
    from mypy_boto3_ec2.service_resource import Instance
    from mypy_boto3_emr import EMRClient

Env().read_env()  # Load .env file
# CSV of issuer,client
OPKSSH_PROVIDER_TAG = 'opkssh_provider'
# EMR doesn't support comma's in tags.
OPKSSH_ISSUER_TAG = 'opkssh_issuer'
OPKSSH_CLIENT_TAG = 'opkssh_client'


class ShellError(Exception):
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message, exit_code)
        self.message: str = message
        self.exit_code: int = exit_code


def set_terminal_title(title: str = ''):
    if os.name == 'nt':
        # Windows - CMD
        os.system(f'title "{title}"')  # noqa: S605

        # Windows - Powershell - But it seems that the scripts are always ran inside CMD anyway.
        # os.system(f'$host.UI.RawUI.WindowTitle = {title}')
    else:
        # Unix
        if os.getenv('TMUX'):
            # Set the TMUX Window title
            sys.stdout.write(f"\33k{title}\33")
        else:
            # Set the Gnome terminal title
            sys.stdout.write(f"\33]0;{title}\a")
        sys.stdout.flush()


@dataclass
class SelectedEMRInstance:
    cluster_id: str
    cluster_name: str
    group_name: str
    group_idx: int
    ip: IP


@click.group()
@click.option('-ll/', '--long-log/--no-long-log', default=False, help='Enable long logging')
@click.option('--verbose/--no-verbose', default=False, help='Enable debug logging')
@click.option('--quiet/--no-quiet', default=False, help='Disable logging')
def cli(
    long_log: bool = False,
    verbose: bool = False,
    quiet: bool = False,
    **kwargs: Any,
):
    log_format = (
        (
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
        )
        if long_log
        else "<level>{message}</level>"
    )
    level = "DEBUG" if verbose else 100 if quiet else "INFO"

    logger.remove()
    logger.add(sys.stdout, level=level, format=log_format, colorize=True)


@cli.command('ec2')
@click.option('-p', '--profile', default=None, help='Which AWS profile to use')
@click.option('-r', '--region', default=None, help='Which AWS region to use')
@click.option('-u', '--user', default=None, help='Which user to connect as')
@click.option('--private/--public', default=True, help="Connect to the instance's private or public IP")
@click.option('-k/', '--key-file', default=None, help="Which key file to use to connect")
def ec2_ssh(
    profile: str | None = None,
    region: str | None = None,
    user: str | None = None,
    private: bool = True,
    key_file: str | None = None,
    **kwargs: Any,
):
    '''
    Asks user which EC2 instance they want to connect to,
    then opens an interactive SSH session to the instance

    '''
    try:
        b3s = boto3.Session(profile_name=profile, region_name=region)

        ip, user, key_file, instance_name = get_ec2_ssh_options(
            b3s=b3s,
            user=user,
            use_private_ip=private,
            key_file=key_file,
        )
        terminal_title = f'{user}@{instance_name}'

        SSHShell(
            hostname=ip,
            username=user,
            key_filename=key_file,
            terminal_title=terminal_title,
        ).connect()
    except ShellError as e:
        logger.error(e.message)
        exit(e.exit_code)
    except ClientError as e:
        if e.response.get('Error') and e.response.get('Error', {}).get('Code') == 'ExpiredTokenException':
            logger.log(
                logging.CRITICAL,
                'Your AWS Token has expired. Please update and try again.',
            )
            exit(1)


@cli.command('emr')
@click.option('-p', '--profile', default=None, help='Which AWS profile to use')
@click.option('-r', '--region', default=None, help='Which AWS region to use')
@click.option('-u', '--user', default=None, help='Which user to connect as')
@click.option('--private/--public', default=True, help="Connect to the instance's private or public IP")
@click.option('-k/', '--key-file', default=None, help="Which key file to use to connect")
def emr_ssh(
    profile: str | None = None,
    region: str | None = None,
    user: str | None = None,
    private: bool = True,
    key_file: str | None = None,
    **kwargs: Any,
):
    '''
    Asks user which Cluster and EC2 instance they want to connect to,
    then opens an interactive SSH session to the instance
    '''
    try:
        b3s = boto3.Session(profile_name=profile, region_name=region)
        emr = b3s.client("emr")

        emr_instance = get_emr_ssh_options(emr)

        if user is None:
            user = 'hadoop'

        if key_file is None:
            key_file = get_emr_ssh_key_file_for_cluster(
                emr,
                emr_instance.cluster_id,
            )

        if private:
            ip = emr_instance.ip.private
            if ip is None:
                raise ShellError(
                    'The selected instance does not have a private IP',
                )
        else:
            ip = emr_instance.ip.public
            if ip is None:
                raise ShellError(
                    'The selected instance does not have a public IP',
                )

        group_name = emr_instance.group_name.split(' ')[0]
        postfix = f'[{emr_instance.group_idx}]' or ''
        terminal_title = f'{emr_instance.cluster_name} - {group_name}{postfix}'

        with SSHShell(hostname=ip, username=user, key_filename=key_file, terminal_title=terminal_title) as shell:
            if group_name.lower().startswith('core'):
                shell.send(b'sudo su\r\n')
                shell.send(b'cd /var/log/hadoop-yarn/containers\r\n')

        set_terminal_title()

    except ShellError as e:
        logger.error(e.message)
        exit(e.exit_code)
    except ClientError as e:
        if e.response.get('Error') and e.response.get('Error', {}).get('Code') == 'ExpiredTokenException':
            logger.log(
                logging.CRITICAL,
                'Your AWS Token has expired. Please update and try again.',
            )
            exit(1)


@cli.command('emr-all')
@click.option('-p', '--profile', default=None, help='Which AWS profile to use')
@click.option('-r', '--region', default=None, help='Which AWS region to use')
@click.option('-u', '--user', default=None, help='Which user to connect as')
@click.option('--private/--public', default=True, help="Connect to the instance's private or public IP")
@click.option('-k/', '--key-file', default=None, help="Which key file to use to connect")
def emr_ssh_all(
    profile: str | None = None,
    region: str | None = None,
    user: str | None = None,
    private: bool = True,
    key_file: str | None = None,
    **kwargs: Any,
):
    '''
    Asks user which Cluster and EC2 instance they want to connect to,
    Then prints a tmux cli statement that will open a new session
    with a window per ec2 instance with ssh shell already opened.
    '''
    try:
        b3s = boto3.Session(profile_name=profile, region_name=region)
        emr = b3s.client('emr')
        cluster_id, cluster_name = prompt_for_emr_cluster(emr)
        grouped_instances = {k.split(" ")[0]: v for k, v in get_emr_instance_ips(emr, cluster_id).items()}

        if user is None:
            user = 'hadoop'

        if key_file is None:
            key_file = get_emr_ssh_key_file_for_cluster(emr, cluster_id)

        window_order = ['Master', 'Primary', 'Core', 'Task']

        def get_ip(ip: IP) -> str | None:
            if private:
                return ip.private
            else:
                if ip.public is None:
                    raise ShellError(
                        f'The instance with private IP {ip.private} does not have a public IP',
                    )
                return ip.public

        window_cmds = [
            # Add "|| $SHELL -i" so that if the ssh session fails, we still have a window so we can look at the error.
            # without this, tmux will close the window.
            (
                f'{group_name} - {instance_num}',
                f'ssh -i {key_file} {user}@{get_ip(instance_ip)} || $SHELL -i',
            )
            for group_name, instance_ips in sorted(grouped_instances.items(), key=lambda t: window_order.index(t[0]))
            for instance_num, instance_ip in enumerate(instance_ips, start=1)
        ]

        session_name = cluster_name
        first_window_name, first_cmd = window_cmds[0]
        # Create a new tmux session with the EMR cluster name as the session name and open the first ssh connection
        tmux(
            f'new-session -d -s "{session_name}" -n "{first_window_name}" "{first_cmd}"',
        )
        # Create a new tmux window and open the ssh connections
        for window_cmd in window_cmds[1:]:
            tmux(
                f'new-window -t "{session_name}:" -n "{window_cmd[0]}" "{window_cmd[1]}"',
            )
        # Now switch to the session's first window
        tmux(f'switch-client -t "{session_name}:{first_window_name}"')

    except ShellError as e:
        logger.error(e.message)
        exit(e.exit_code)
    except ClientError as e:
        if e.response.get('Error') and e.response.get('Error', {}).get('Code') == 'ExpiredTokenException':
            logger.log(
                logging.CRITICAL,
                'Your AWS Token has expired. Please update and try again.',
            )
            exit(1)


@dataclass
class SSHShell:
    hostname: str
    username: str
    key_filename: str | None = None
    terminal_title: str | None = None

    private_key: paramiko.PKey = field(init=False)
    _ssh_client: paramiko.SSHClient = field(init=False)
    _channel: paramiko.Channel = field(init=False)

    def __post_init__(self):
        if self.key_filename is not None:
            key_filename = Path(self.key_filename)
            if not key_filename.exists():
                raise ValueError(f'File {self.key_filename} does not exist')

            self.private_key = paramiko.PKey.from_path(key_filename)
            # Load first public key - opkssh used to postfix with ".pub", but now uses "-cert.pub"
            for postfix in (".pub", "-cert.pub"):
                stem_postfix, suffix = postfix.split('.', maxsplit=1)
                public_key_path = key_filename.with_stem(f"{key_filename.stem}{stem_postfix}").with_suffix(f".{suffix}")
                if public_key_path.exists() and public_key_path.is_file():
                    self.private_key.load_certificate(str(public_key_path.absolute()))
                    break

    def connect(self):
        self._open()
        self._launch()
        self._close()

    def _open(self) -> paramiko.Channel:
        logger.info(f'Opening SSH: ssh -i {self.key_filename} {self.username}@{self.hostname}')

        terminal_size = os.get_terminal_size()

        ssh_client = paramiko.SSHClient()
        # Set hosts key path so we can save to it
        host_key_path = os.path.expanduser('~/.ssh/known_hosts')
        ssh_client.load_host_keys(host_key_path)
        ssh_client.set_missing_host_key_policy(ConfirmAddPolicy())
        try:
            ssh_client.connect(
                hostname=self.hostname,
                username=self.username,
                pkey=self.private_key,
            )
        except paramiko.BadHostKeyException as e:
            error_message = f'''
                @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
                @    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @
                @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
                IT IS POSSIBLE THAT SOMEONE IS DOING SOMETHING NASTY!
                Someone could be eavesdropping on you right now (man-in-the-middle attack)!
                It is also possible that a host key has just been changed.
                The fingerprint for the key sent by the remote host is
                {e.key.fingerprint}
                Expected key is
                {e.expected_key.fingerprint}
                Please contact your system administrator.
                Add correct host key in {host_key_path} to get rid of this message.
                Offending key in {host_key_path}
                  remove with:
                  ssh-keygen -f "{host_key_path}" -R "{self.hostname}"
                Host key for {self.hostname} has changed and you have requested strict checking.
                Host key verification failed.
            '''
            raise ShellError(textwrap.dedent(error_message), 255) from None

        channel = ssh_client.get_transport().open_session()  # pyright: ignore[reportOptionalMemberAccess]
        channel.get_pty(
            term=os.getenv('TERM', 'xterm-256color'),
            width=terminal_size.columns,
            height=terminal_size.lines,
        )
        channel.invoke_shell()

        self._ssh_client = ssh_client
        self._channel = channel
        if self.terminal_title:
            set_terminal_title(self.terminal_title)

        return channel

    def _launch(self):
        interactive_shell(
            self._channel,
            allow_title_changes=not self.terminal_title,
        )

    def _close(self):
        self._ssh_client.close()
        # Reset on connection close
        if self.terminal_title:
            set_terminal_title()

    def __enter__(self):
        return self._open()

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception_value: BaseException | None,
        traceback: TracebackType | None,
    ):
        self._launch()
        self._close()


def tmux(command: str):
    os.system(f'tmux {command}')  # noqa: S605


def try_to_find_ssh_key_file(key_name: str) -> str | None:
    '''Recursively iterate over the `~/.ssh/` folder to find the matching key'''
    if key_name:
        for dirpath, _, filenames in os.walk(os.path.expanduser('~/.ssh/')):
            if filenames:
                for f in filenames:
                    if f == key_name or os.path.splitext(f)[0] == key_name:
                        return os.path.join(dirpath, f)

    return None


def get_ec2_image_name(instance: "Instance") -> str | None:
    try:
        return instance.image.name
    except AttributeError:
        return None


def get_ec2_ssh_options(
    b3s: boto3.Session,
    user: str | None = None,
    use_private_ip: bool = True,
    key_file: str | None = None,
) -> tuple[str, str, str | None, str]:
    instance = prompt_for_ec2_instance(b3s)
    instance_tags = {tag.get('Key', '').lower(): tag.get('Value', '') for tag in instance.tags}

    if key_file is None:
        # Support https://github.com/openpubkey/opkssh via tags
        if OPKSSH_PROVIDER_TAG in instance_tags:
            key_file = get_opkssh_key_file(instance_tags[OPKSSH_PROVIDER_TAG])
        elif OPKSSH_ISSUER_TAG in instance_tags and OPKSSH_CLIENT_TAG in instance_tags:
            key_file = get_opkssh_key_file(
                ','.join(
                    [
                        instance_tags[OPKSSH_ISSUER_TAG],
                        instance_tags[OPKSSH_CLIENT_TAG],
                    ],
                ),
            )
        else:
            key_file = try_to_find_ssh_key_file(instance.key_name)

    if user is None:
        logger.info('No user specified, attempting to detect required user...')
        image_name = get_ec2_image_name(instance)
        user = 'ubuntu' if image_name and 'ubuntu' in image_name.lower() else 'ec2-user'

    if use_private_ip:
        ip = instance.private_ip_address
    elif instance.public_ip_address:
        ip = instance.public_ip_address
    else:
        raise ShellError('Public IP was requested, but none was found!')

    return ip, user, key_file, get_ec2_name(instance)


def get_emr_ssh_options(
    emr: "EMRClient",
) -> SelectedEMRInstance:
    cluster_id, cluster_name = prompt_for_emr_cluster(emr)
    instance_ips, group_name = prompt_for_emr_instance_group(emr, cluster_id)

    instance_ips = sorted(instance_ips, key=lambda ips: ips.private or "")

    instance_options = [f'{ip.private} ({ip.public})' if ip.public else f'{ip.private}' for ip in instance_ips]
    instance_ip = questionary.select(
        'Which instance do you want to connect to?',
        choices=instance_options,
    ).unsafe_ask()
    group_idx = instance_options.index(instance_ip)

    return SelectedEMRInstance(cluster_id, cluster_name, group_name, group_idx, instance_ips[group_idx])


def get_emr_ssh_key_file_for_cluster(
    emr: "EMRClient",
    cluster_id: str,
) -> str | None:
    with click_spinner.spinner():
        cluster = emr.describe_cluster(ClusterId=cluster_id)
        cluster_tags = {tag.get('Key', '').lower(): tag.get('Value', '') for tag in cluster['Cluster'].get('Tags', [])}

        key_file = key_name = None
        # Support https://github.com/openpubkey/opkssh via tags
        if OPKSSH_PROVIDER_TAG in cluster_tags:
            key_file = get_opkssh_key_file(cluster_tags[OPKSSH_PROVIDER_TAG])
        elif OPKSSH_ISSUER_TAG in cluster_tags and OPKSSH_CLIENT_TAG in cluster_tags:
            key_file = get_opkssh_key_file(
                ','.join(
                    [
                        cluster_tags[OPKSSH_ISSUER_TAG],
                        cluster_tags[OPKSSH_CLIENT_TAG],
                    ],
                ),
            )

        if key_file is None:
            key_name = cluster["Cluster"].get("Ec2InstanceAttributes", {}).get("Ec2KeyName", "")
            key_file = try_to_find_ssh_key_file(key_name)

        if key_file is None:
            should_continue = questionary.confirm(
                f'Could not find the ssh key {key_name}, would you like to continue?',
            ).unsafe_ask()
            if not should_continue:
                exit(1)

    return key_file


def get_running_ec2_instances(b3s: boto3.Session):
    '''Discover running instances'''
    with click_spinner.spinner():
        ec2 = b3s.resource('ec2')
        running_instances = ec2.instances.filter(
            Filters=[{'Name': 'instance-state-name', 'Values': ['running']}],
        )

    return running_instances


def prompt_for_ec2_instance(
    b3s: boto3.Session,
    prompt: str = 'Which EC2 instance do you want to connect to?',
) -> "Instance":
    '''
    Discovers the runnin EC2 clusters and asks the user which to use.

    Returns the EC2 object that the user selected.
    '''
    running_instances = get_running_ec2_instances(b3s)

    name_contains = (
        questionary.text('Provide a name filter or leave blank to show all')  #
        .unsafe_ask()
        .lower()
    )

    with click_spinner.spinner():
        grouped_by_name = {
            get_ec2_name(instance): instance
            for instance in running_instances
            if not name_contains or name_contains in get_ec2_name(instance).lower()
        }

        if not grouped_by_name:
            raise ShellError('No matching EC2 instances found!')

    # Prompt the user
    ec2_name = questionary.select(
        prompt,
        choices=sorted(grouped_by_name),
    ).unsafe_ask()
    instance = grouped_by_name[ec2_name]

    return instance


def get_ec2_name(ec2_instance: "Instance") -> str:
    '''Takes in the boto3 EC2 resource instance object'''
    for tag in ec2_instance.tags:
        if tag.get('Key') == 'Name':
            return tag.get('Value', '')

    return ec2_instance.instance_id


def get_opkssh_key_file(provider: str) -> str:
    opkssh_key_file = Path.home() / '.ssh/' / f'opkssh_{sha256(provider.encode()).hexdigest()}'

    if opkssh_key_file.exists() and opkssh_key_file.stat().st_mtime > dt.datetime.now().timestamp() - 86400:
        logger.info('Found existing opkssh key')
    else:
        if opkssh_key_file.exists() and opkssh_key_file.is_file():
            logger.info("Detected expired opkssh key, deleting")
            # Delete public keys - opkssh used to postfix with ".pub", but now uses "-cert.pub"
            for postfix in (".pub", "-cert.pub"):
                stem_postfix, suffix = postfix.split('.', maxsplit=1)
                public_key_path = opkssh_key_file.with_stem(f"{opkssh_key_file.stem}{stem_postfix}").with_suffix(f".{suffix}")
                if public_key_path.exists() and public_key_path.is_file():
                    public_key_path.unlink()

            # Delete private key
            opkssh_key_file.unlink()

        logger.info('Detected opkssh, logging in')
        cmd = [
            "opkssh",
            "login",
            "--provider",
            provider,
            "-i",
            str(opkssh_key_file.absolute()),
        ]
        process = subprocess.Popen(cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # noqa: S603
        process.wait()
        if process.returncode != 0:
            logger.error('Failed to authenticate using opkssh')
            if process.stdout:
                logger.info(process.stdout.read().decode())
            if process.stderr:
                logger.error(process.stderr.read().decode())
            sys.exit(process.returncode)

    return str(opkssh_key_file.absolute())


class ConfirmAddPolicy(paramiko.client.MissingHostKeyPolicy):
    """
    Policy for automatically adding the hostname and new host key to the
    local `.HostKeys` object, and saving it.  This is used by `.SSHClient`.
    """

    @override
    def missing_host_key(self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey):
        logger.warning(
            f"Unknown {key.get_name()} host key for {hostname}: {key.fingerprint}",
        )
        should_add = questionary.confirm(
            "Continue and add host key?",
        ).unsafe_ask()
        if should_add:
            client.get_host_keys().add(hostname, key.get_name(), key)
            host_key_filename: str | None = client._host_keys_filename  # pyright: ignore[reportAttributeAccessIssue]
            if host_key_filename is not None:
                client.save_host_keys(host_key_filename)
                logger.info('Added host key')
            else:
                logger.warning(
                    'Failed to add host key. No host key file defined!',
                )

        else:
            raise paramiko.SSHException(
                f"Server {hostname!r} not found in known_hosts",
            )
