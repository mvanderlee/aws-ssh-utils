import sys
from typing import Any

import click
from loguru import logger
from typing_extensions import override

from aws_ssh_utils.commands.app import app
from aws_ssh_utils.commands.ec2 import ec2
from aws_ssh_utils.commands.emr import emr
from aws_ssh_utils.commands.emr_all import emr_all
from aws_ssh_utils.commands.healthcheck import healthcheck


class DefaultCommandGroup(click.Group):
    """Runs `default_command` when no subcommand is given, e.g. `aws_ssh -p prod` -> `aws_ssh app -p prod`.

    Assumes all group options are flags, so the first argument that isn't one is where the subcommand goes.
    """

    def __init__(self, *args: Any, default_command: str, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.default_command = default_command

    @override
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        group_options = {opt for param in self.get_params(ctx) for opt in (*param.opts, *param.secondary_opts)}
        idx = next((i for i, arg in enumerate(args) if arg not in group_options), len(args))
        if idx == len(args) or args[idx].startswith('-'):
            args = [*args[:idx], self.default_command, *args[idx:]]
        return super().parse_args(ctx, args)


@click.group(cls=DefaultCommandGroup, default_command='app')
@click.option('-ll/', '--long-log/--no-long-log', default=False, help='Enable long logging')
@click.option('--verbose/--no-verbose', default=False, help='Enable debug logging')
@click.option('--quiet/--no-quiet', default=False, help='Disable logging')
def cli(
    long_log: bool = False,
    verbose: bool = False,
    quiet: bool = False,
    **kwargs: Any,
):
    """Runs `app` when no command is given."""
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

    def to_stdout(message: str):
        # Look up sys.stdout per message, so rich.live can redirect it above its display.
        sys.stdout.write(message)

    logger.remove()
    logger.add(to_stdout, level=level, format=log_format, colorize=True)


for command in (app, ec2, emr, emr_all, healthcheck):
    cli.add_command(command)
