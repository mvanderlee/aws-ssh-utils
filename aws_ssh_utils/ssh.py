import sys
from pathlib import Path
from typing import Any

import click
from loguru import logger
from textual.logging import TextualHandler
from typing_extensions import override

import aws_ssh_utils
from aws_ssh_utils.commands.app import app
from aws_ssh_utils.commands.ec2 import ec2
from aws_ssh_utils.commands.emr import emr
from aws_ssh_utils.commands.emr_all import emr_all
from aws_ssh_utils.commands.healthcheck import healthcheck
from aws_ssh_utils.logging_utils import write_console


class DefaultCommandGroup(click.Group):
    """Runs `default_command` when no subcommand is given, e.g. `aws_ssh -p prod` -> `aws_ssh app -p prod`.

    Skip group options and their values before looking for the subcommand.
    """

    def __init__(self, *args: Any, default_command: str, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.default_command = default_command

    @override
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        group_options = {opt: param for param in self.get_params(ctx) for opt in (*param.opts, *param.secondary_opts)}
        idx = 0
        while idx < len(args):
            name, separator, _ = args[idx].partition('=')
            param = group_options.get(name)
            if not isinstance(param, click.Option):
                break
            idx += 1
            if not param.is_flag and not param.count and not separator:
                if len(args) < idx + param.nargs:
                    return super().parse_args(ctx, args)
                idx += param.nargs
        if idx == len(args) or args[idx].startswith('-'):
            args = [*args[:idx], self.default_command, *args[idx:]]
        return super().parse_args(ctx, args)


@click.group(cls=DefaultCommandGroup, default_command='app')
@click.option('-ll/', '--long-log/--no-long-log', default=False, help='Enable long logging')
@click.option('--verbose/--no-verbose', default=False, help='Enable debug logging')
@click.option('--quiet/--no-quiet', default=False, help='Disable console logging')
@click.option(
    '--log-file',
    type=click.Path(dir_okay=False, path_type=Path),
    envvar='AWS_SSH_LOG_FILE',
    help='Append diagnostics and tracebacks to this file. Also settable with AWS_SSH_LOG_FILE.',
)
@click.pass_context
def cli(
    ctx: click.Context,
    long_log: bool = False,
    verbose: bool = False,
    quiet: bool = False,
    log_file: Path | None = None,
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
    level = 100 if quiet else "DEBUG" if verbose else "INFO"

    logger.remove()
    # Route to Textual's console when an app is active, and stdout otherwise.
    # Keep the file sink active through both full-screen and questionary flows.
    logger.add(
        write_console,
        level=level,
        format=log_format,
        colorize=True,
        diagnose=False,
        backtrace=False,
        filter=lambda record: not record['extra'].get('file_only'),
    )
    logger.add(
        TextualHandler(stderr=False, stdout=False),
        level=level,
        format='{message}',
        diagnose=False,
        backtrace=False,
        filter=lambda record: not record['extra'].get('file_only'),
    )
    if log_file is not None:
        try:
            sink = logger.add(
                log_file,
                level='DEBUG' if verbose else 'INFO',
                format='{time:YYYY-MM-DD HH:mm:ss.SSS Z} | {level: <8} | {name}:{function}:{line} | {message}',
                encoding='utf-8',
                colorize=False,
                diagnose=False,
                backtrace=False,
                rotation='10 MB',
                retention=3,
            )
        except OSError as error:
            raise click.ClickException(f'Cannot open log file {log_file}: {error}') from error
        ctx.call_on_close(lambda: logger.remove(sink))
        logger.bind(file_only=True).info(
            'aws-ssh-utils {}; Python {}; source={}',
            aws_ssh_utils.__version__,
            sys.version.split()[0],
            aws_ssh_utils.__file__,
        )


for command in (app, ec2, emr, emr_all, healthcheck):
    cli.add_command(command)
