"""SSH connection strategies, tried in order until one succeeds."""

import os
import shlex
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from fnmatch import fnmatch
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import IO, Literal, cast

import paramiko
from loguru import logger
from typing_extensions import override

# CSV of issuer,client
OPKSSH_PROVIDER_TAG = 'opkssh_provider'
# EMR doesn't support comma's in tags.
OPKSSH_ISSUER_TAG = 'opkssh_issuer'
OPKSSH_CLIENT_TAG = 'opkssh_client'
OPKSSH_KEY_MAX_AGE = 86400
CONNECT_TIMEOUT = 15
SSH_DIR = Path.home() / '.ssh'

OnOutput = Callable[[str], None]
ConfirmHostKey = Callable[[str, paramiko.PKey], bool]


@dataclass(frozen=True)
class Target:
    instance_id: str
    ip: str
    user: str
    aliases: tuple[str, ...] = ()
    """Extra names to look up in ~/.ssh/config, e.g. the EC2 Name tag."""
    key_name: str | None = None
    key_file: str | None = None
    """Explicit key file, used instead of looking up `key_name` in ~/.ssh."""
    opkssh_provider: str | None = None


@dataclass(frozen=True)
class Environment:
    ssm_installed: bool
    opkssh_installed: bool
    profile: str | None = None
    region: str | None = None


@dataclass(frozen=True)
class Connection:
    client: paramiko.SSHClient
    via: str
    """How the connection was made: AWS SSM, opkssh, a key name, ..."""


@dataclass(frozen=True)
class Attempt:
    via: str
    command: str
    """The equivalent command line, to retry by hand."""
    open_client: Callable[[], paramiko.SSHClient] = field(repr=False, compare=False)
    state: Literal['pending', 'connecting', 'connected', 'failed', 'skipped'] = 'pending'
    detail: str = ''
    """The error when failed, the reason when skipped."""


OnProgress = Callable[[list[Attempt]], None]


def connect(
    target: Target,
    env: Environment,
    on_output: OnOutput,
    on_waiting: Callable[[], None],
    on_progress: OnProgress,
    confirm_host_key: ConfirmHostKey | None = None,
) -> Connection | None:
    """Return the first successful connection, or None when all fail.

    Tried in order:
    1. ~/.ssh/config, when a Host pattern (other than `*`) matches the instance id, an alias or the IP.
    2. SSH over AWS SSM to the instance id, with any existing opkssh/EC2 key plus the default keys.
    3. opkssh, when the instance/cluster has opkssh tags. May open a browser login.
    4. The EC2 key pair (or `target.key_file`), when a matching file is found in ~/.ssh.
    5. The ssh-agent and default ~/.ssh/id_* keys.

    `on_progress` gets a snapshot of all attempts whenever one changes state.
    Unknown host keys are trusted, unless `confirm_host_key` declines them.
    """
    open_ssh_ = partial(open_ssh, confirm_host_key=confirm_host_key)
    attempts: list[Attempt] = []

    config = find_ssh_config(target)
    if config is not None:
        alias, options = config
        attempts.append(
            Attempt(
                f"ssh config ({alias})",
                shlex.join(['ssh', alias]),
                lambda: open_with_config(alias, options, target, confirm_host_key),
            ),
        )

    ec2_key = target.key_file or find_ssh_key_file(target.key_name)
    known_keys = [k for k in (valid_opkssh_key_file(target.opkssh_provider), ec2_key) if k]
    ssm_proxy = ssm_command(target.instance_id, env)
    ssm = Attempt(
        "AWS SSM",
        ssh_command(target.user, target.instance_id, known_keys, proxy=ssm_proxy),
        lambda: open_ssh_(
            target.instance_id,
            target.user,
            known_keys,
            use_defaults=True,
            sock=proxy_socket(ssm_proxy, on_output),
        ),
    )
    attempts.append(ssm if env.ssm_installed else replace(ssm, state='skipped', detail="session-manager-plugin not installed"))

    if (provider := target.opkssh_provider) is not None:
        opkssh_key = str(opkssh_key_path(provider))
        opkssh = Attempt(
            "opkssh",
            f"{shlex.join(opkssh_login_command(provider, opkssh_key))} && "
            + ssh_command(target.user, target.ip, [opkssh_key], identities_only=True),
            lambda: open_ssh_(target.ip, target.user, [opkssh_login(provider, on_output, on_waiting)], use_defaults=False),
        )
        attempts.append(opkssh if env.opkssh_installed else replace(opkssh, state='skipped', detail="opkssh not installed"))

    if ec2_key is not None:
        attempts.append(
            Attempt(
                Path(ec2_key).stem,
                ssh_command(target.user, target.ip, [ec2_key], identities_only=True),
                lambda: open_ssh_(target.ip, target.user, [ec2_key], use_defaults=False),
            ),
        )

    attempts.append(
        Attempt(
            "default key",
            ssh_command(target.user, target.ip),
            lambda: open_ssh_(target.ip, target.user, [], use_defaults=True),
        ),
    )

    for i, attempt in enumerate(attempts):
        if attempt.state == 'skipped':
            continue
        attempts[i] = replace(attempt, state='connecting')
        on_progress(list(attempts))
        try:
            client = attempt.open_client()
        except Exception as e:
            logger.debug(f"Failed to connect via {attempt.via}: {e!r}")
            attempts[i] = replace(attempt, state='failed', detail=str(e) or type(e).__name__)
            on_progress(list(attempts))
            continue
        attempts[i] = replace(attempt, state='connected')
        on_progress(list(attempts))
        return Connection(client, attempt.via)
    return None


def open_ssh(
    hostname: str,
    username: str,
    key_files: Sequence[str],
    *,
    use_defaults: bool,
    sock: socket.socket | None = None,
    port: int = 22,
    confirm_host_key: ConfirmHostKey | None = None,
) -> paramiko.SSHClient:
    """`use_defaults` enables the ssh-agent and ~/.ssh/id_* keys, like OpenSSH without IdentitiesOnly."""
    known_hosts = SSH_DIR / 'known_hosts'
    known_hosts.parent.mkdir(mode=0o700, exist_ok=True)
    known_hosts.touch(exist_ok=True)

    client = paramiko.SSHClient()
    client.load_host_keys(str(known_hosts))
    client.set_missing_host_key_policy(AcceptNewPolicy(known_hosts, port, confirm_host_key))
    try:
        client.connect(
            hostname,
            port=port,
            username=username,
            key_filename=list(key_files) or None,  # pyright: ignore[reportArgumentType]
            allow_agent=use_defaults,
            look_for_keys=use_defaults,
            sock=sock,  # pyright: ignore[reportArgumentType]
            timeout=CONNECT_TIMEOUT,
            banner_timeout=CONNECT_TIMEOUT,
            auth_timeout=CONNECT_TIMEOUT,
        )
    except BaseException:
        client.close()
        if sock is not None:
            sock.close()
        raise
    return client


def ssh_command(
    user: str,
    host: str,
    key_files: Sequence[str] = (),
    *,
    identities_only: bool = False,
    proxy: list[str] | None = None,
) -> str:
    """The OpenSSH command line equivalent of an `open_ssh` call."""
    cmd = ['ssh']
    if proxy:
        cmd += ['-o', f'ProxyCommand={shlex.join(proxy)}']
    if identities_only:
        cmd += ['-o', 'IdentitiesOnly=yes']
    for key_file in key_files:
        cmd += ['-i', key_file]
    return shlex.join([*cmd, f'{user}@{host}'])


def open_shell_channel(client: paramiko.SSHClient) -> paramiko.Channel:
    channel = client.get_transport().open_session()  # pyright: ignore[reportOptionalMemberAccess]
    channel.get_pty(term='xterm-256color')
    channel.invoke_shell()
    return channel


class AcceptNewPolicy(paramiko.MissingHostKeyPolicy):
    """OpenSSH's `StrictHostKeyChecking=accept-new`: trust unknown hosts, still reject changed keys.

    With `confirm`, unknown hosts are only trusted when it returns True.
    """

    def __init__(self, known_hosts: Path, port: int, confirm: ConfirmHostKey | None = None):
        self.known_hosts = known_hosts
        self.port = port
        self.confirm = confirm

    @override
    def missing_host_key(self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey):
        if self.confirm is not None and not self.confirm(hostname, key):
            raise paramiko.SSHException(f"Server {hostname!r} not found in known_hosts")
        logger.info(f"Adding {key.get_name()} host key for {hostname}: {key.fingerprint}")
        client.get_host_keys().add(hostname, key.get_name(), key)
        host = hostname if self.port == 22 else f"[{hostname}]:{self.port}"
        # Append instead of save_host_keys(), which would rewrite the user's known_hosts.
        with self.known_hosts.open('a') as f:
            f.write(f"{host} {key.get_name()} {key.get_base64()}\n")


# region - ssh config
def find_ssh_config(target: Target) -> tuple[str, paramiko.SSHConfigDict] | None:
    """Return the first target alias that an explicit (non `*`) Host pattern in ~/.ssh/config matches."""
    path = SSH_DIR / 'config'
    if not path.is_file():
        return None

    config = paramiko.SSHConfig.from_path(str(path))
    patterns = [p for p in config.get_hostnames() if p != '*']
    for alias in (target.instance_id, *target.aliases, target.ip):
        if alias and any(fnmatch(alias, p) for p in patterns):
            return alias, config.lookup(alias)
    return None


def open_with_config(
    alias: str,
    options: paramiko.SSHConfigDict,
    target: Target,
    confirm_host_key: ConfirmHostKey | None = None,
) -> paramiko.SSHClient:
    proxy = options.get('proxycommand')
    identities_only = options.get('identitiesonly', 'no').lower() == 'yes'
    return open_ssh(
        options.get('hostname', alias),
        options.get('user', target.user),
        [os.path.expanduser(f) for f in options.get('identityfile', [])],
        use_defaults=not identities_only,
        sock=proxy_socket(proxy, lambda _: None) if proxy and proxy.lower() != 'none' else None,
        port=int(options.get('port', 22)),
        confirm_host_key=confirm_host_key,
    )


# endregion - ssh config


# region - SSM
def ssm_command(instance_id: str, env: Environment) -> list[str]:
    cmd = [
        'aws',
        'ssm',
        'start-session',
        '--target',
        instance_id,
        '--document-name',
        'AWS-StartSSHSession',
        '--parameters',
        'portNumber=22',
    ]
    if env.profile:
        cmd += ['--profile', env.profile]
    if env.region:
        cmd += ['--region', env.region]
    return cmd


def proxy_socket(command: str | list[str], on_stderr: OnOutput) -> socket.socket:
    """Run a ProxyCommand and return a socket connected to its stdio.

    paramiko.ProxyCommand select()s on pipes, which Windows doesn't support, so bridge through a socketpair.
    A string command runs through the shell, like OpenSSH's ProxyCommand.
    """
    ours, theirs = socket.socketpair()
    if isinstance(command, str):
        logger.debug(f"ProxyCommand: {command}")
    else:
        logger.debug(f"ProxyCommand: {shlex.join(command)}")
    proc = subprocess.Popen(  # noqa: S603
        command,
        shell=isinstance(command, str),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Popen with PIPE always sets these
    stdin, stdout, stderr = cast('tuple[IO[bytes], IO[bytes], IO[bytes]]', (proc.stdin, proc.stdout, proc.stderr))

    def to_socket():
        try:
            while data := os.read(stdout.fileno(), 65536):
                theirs.sendall(data)
        except OSError:
            pass
        # shutdown, not close: closing while to_process() is in recv() resets the connection on Windows
        theirs.shutdown(socket.SHUT_WR)

    def to_process():
        try:
            while data := theirs.recv(65536):
                stdin.write(data)
                stdin.flush()
        except OSError:
            pass
        proc.kill()
        theirs.close()

    def log_stderr():
        for line in stderr:
            on_stderr(line.decode(errors='replace').rstrip())

    for pump in (to_socket, to_process, log_stderr):
        threading.Thread(target=pump, daemon=True).start()
    return ours


# endregion - SSM


# region - keys
def find_ssh_key_file(key_name: str | None) -> str | None:
    """Recursively search ~/.ssh for a file named `key_name`, with or without extension."""
    if not key_name:
        return None
    for dirpath, _, filenames in os.walk(SSH_DIR):
        for f in filenames:
            if f == key_name or os.path.splitext(f)[0] == key_name:
                return os.path.join(dirpath, f)
    return None


def opkssh_provider(tags: dict[str, str]) -> str | None:
    """Tags keys must be lower-cased."""
    if OPKSSH_PROVIDER_TAG in tags:
        return tags[OPKSSH_PROVIDER_TAG]
    if OPKSSH_ISSUER_TAG in tags and OPKSSH_CLIENT_TAG in tags:
        return f"{tags[OPKSSH_ISSUER_TAG]},{tags[OPKSSH_CLIENT_TAG]}"
    return None


def opkssh_key_path(provider: str) -> Path:
    return SSH_DIR / f'opkssh_{sha256(provider.encode()).hexdigest()}'


def valid_opkssh_key_file(provider: str | None) -> str | None:
    if provider is None:
        return None
    key = opkssh_key_path(provider)
    return str(key) if key.is_file() and key.stat().st_mtime > time.time() - OPKSSH_KEY_MAX_AGE else None


def opkssh_login_command(provider: str, key_file: str) -> list[str]:
    return ['opkssh', 'login', '--provider', provider, '-i', key_file]


def opkssh_login(provider: str, on_output: OnOutput, on_waiting: Callable[[], None]) -> str:
    """Return a valid opkssh key file, logging in when missing or expired."""
    if key_file := valid_opkssh_key_file(provider):
        return key_file

    key = opkssh_key_path(provider)
    # opkssh used to postfix the public key with ".pub", but now uses "-cert.pub"
    for path in (key, key.with_name(f"{key.name}.pub"), key.with_name(f"{key.name}-cert.pub")):
        path.unlink(missing_ok=True)

    proc = subprocess.Popen(  # noqa: S603
        opkssh_login_command(provider, str(key)),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    for line in cast('IO[str]', proc.stdout):
        on_output(line.rstrip())
        if 'http://' in line or 'https://' in line:
            on_waiting()
    if proc.wait() != 0:
        raise RuntimeError(f"opkssh login exited with code {proc.returncode}")
    return str(key)


# endregion - keys
