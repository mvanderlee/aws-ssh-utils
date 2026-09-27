import re
import subprocess
import sys
from collections.abc import Callable
from shutil import which
from typing import TYPE_CHECKING, TypeVar

import boto3
import click
import requests
from botocore.exceptions import BotoCoreError, ClientError
from loguru import logger
from rich.console import Console, RenderableType
from rich.progress import Progress, SpinnerColumn, Task, TextColumn
from rich.table import Column
from rich.text import Text
from typing_extensions import override

if TYPE_CHECKING:
    from mypy_boto3_ec2.service_resource import EC2ServiceResource
    from mypy_boto3_emr import EMRClient


AWS_SSM_LATEST_RELEASE_URL = "https://api.github.com/repos/aws/session-manager-plugin/releases/latest"
OPKSSH_VERSION_RE = re.compile(r".*([0-9]+\.[0-9]+\.[0-9]+)")
"""OPKSSH shows version as 'opkssh version 0.16.0'. We only want the actual version."""
ACCESS_DENIED_ERROR_CODES = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}
PROBE_EMR_CLUSTER_ID = "j-0000000000000"
"""Non-existent cluster: EMR replies InvalidRequestException when allowed, AccessDeniedException when not."""

T = TypeVar("T")


@click.command("healthcheck")
@click.option('-p', '--profile', default=None, help='Which AWS profile to use. Used to verify permissions.')
@click.option('-r', '--region', default=None, help='Which AWS region to use. Used to verify permissions.')
def healthcheck(
    profile: str | None = None,
    region: str | None = None,
):
    try:
        boto3_session = boto3.Session(profile_name=profile, region_name=region)
    except BotoCoreError as e:
        logger.debug(f"AWS session could not be created: {e}")
        boto3_session = None

    console = Console()
    icons = _icons(console)

    with Progress(StatusColumn(icons), TextColumn("{task.description}"), console=console) as progress:

        def run(
            description: str,
            outcome: tuple[str, str],
            check: Callable[[], T],
            passed: Callable[[T], bool] = bool,
        ) -> T:
            """Show `description` while `check` runs, then `outcome` as (pass, fail) text."""
            task = progress.add_task(description, total=1)
            result = check()
            ok = passed(result)
            progress.update(task, completed=1, description=outcome[not ok], icon=icons["pass" if ok else "fail"])
            return result

        aws_auth_passed = run(
            "Checking AWS authentication",
            ("AWS authenticated", "AWS authentication failed"),
            lambda: boto3_session is not None and is_aws_authenticated(boto3_session),
        )
        if aws_auth_passed and boto3_session is not None:
            aws_permissions = run(
                "Checking AWS permissions",
                ("AWS permissions granted", "AWS permissions missing"),
                lambda: list_missing_aws_permissions(boto3_session),
                passed=lambda missing: not missing,
            )
        else:
            aws_permissions = []
            task = progress.add_task("[dim]AWS permissions skipped[/]", total=1)
            progress.update(task, completed=1, icon=icons["skip"])
        aws_ssm_passed = run("Checking AWS SSM", ("AWS SSM installed", "AWS SSM missing"), is_aws_ssm_installed)
        opkssh_passed = run("Checking opkssh", ("opkssh installed", "opkssh missing"), is_opkssh_installed)

    console.print("---")
    if aws_auth_passed and not aws_permissions and aws_ssm_passed and opkssh_passed:
        console.print(f"{icons['pass']} All checks passed.")
        return

    required_passed = aws_auth_passed and not aws_permissions
    if not required_passed:
        console.print("Required AWS checks failed")
        if not aws_auth_passed:
            console.print(f"  {icons['fail']} AWS Authentication failed. Please verify your AWS credentials!")
        if aws_permissions:
            console.print(
                f"  {icons['fail']} You do not have the required IAM permissions, the following IAM permissions are missing:",
            )
            console.print("\n".join(f"    - {x}" for x in aws_permissions))

    optional_passed = aws_ssm_passed and opkssh_passed
    if not optional_passed:
        if not required_passed:
            console.print("")

        console.print("Optional checks failed")

        if not aws_ssm_passed:
            console.print(f"  {icons['fail']} AWS session-manager-plugin can not be detected.")
            console.print(
                "    To install AWS SSM, please see:\n"
                "    https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html",
                soft_wrap=True,
            )

        if not opkssh_passed:
            console.print(f"  {icons['fail']} opkssh can not be detected.")
            console.print("    To install opkssh, please see:\n  https://github.com/openpubkey/opkssh#getting-started")

    sys.exit(1)


def _icons(console: Console) -> dict[str, str]:
    if console.legacy_windows or not console.encoding.lower().startswith("utf"):
        return {"spinner": "line", "pass": "[green]OK[/]", "fail": "[red]X[/]", "skip": "[dim]-[/]"}
    return {"spinner": "dots", "pass": "[green]✓[/]", "fail": "[red]✗[/]", "skip": "[dim]-[/]"}


class StatusColumn(SpinnerColumn):
    """Spinner while running, then the task's pass/fail icon."""

    def __init__(self, icons: dict[str, str]):
        super().__init__(icons["spinner"], style="cyan", table_column=Column(width=2))

    @override
    def render(self, task: Task) -> RenderableType:
        return Text.from_markup(task.fields["icon"]) if task.finished else super().render(task)


# region - AWS Auth
def is_aws_authenticated(boto3_session: boto3.Session) -> bool:
    sts = boto3_session.client("sts")
    try:
        identity = sts.get_caller_identity()
    except (BotoCoreError, ClientError) as e:
        logger.debug(f"AWS authentication failed: {e}")
        return False

    logger.debug(f"Authenticated as {identity['Arn']}")
    return True


def list_missing_aws_permissions(boto3_session: boto3.Session) -> list[str]:
    """Detect and return the missing IAM permissions that this tool needs."""
    emr = boto3_session.client("emr")
    ec2 = boto3_session.resource('ec2')

    checks: dict[str, Callable[[], bool]] = {
        "ec2:DescribeInstances": lambda: can_list_ec2_instances(ec2),
        "ec2:DescribeImages": lambda: can_describe_ec2_images(ec2),
        "elasticmapreduce:ListClusters": lambda: can_list_emr_clusters(emr),
        "elasticmapreduce:DescribeCluster": lambda: can_describe_emr_cluster(emr),
        "elasticmapreduce:ListInstanceFleets": lambda: can_list_emr_instance_fleets(emr),
        "elasticmapreduce:ListInstanceGroups": lambda: can_list_emr_instance_groups(emr),
        "elasticmapreduce:ListInstances": lambda: can_list_emr_instances(emr),
    }
    return [permission for permission, check in checks.items() if not check()]


def _is_permitted(call: Callable[[], object]) -> bool:
    """Any outcome other than an access-denied error means IAM allowed the call."""
    try:
        call()
    except ClientError as e:
        return e.response.get("Error", {}).get("Code") not in ACCESS_DENIED_ERROR_CODES

    return True


def can_list_ec2_instances(ec2: "EC2ServiceResource") -> bool:
    return _is_permitted(lambda: ec2.meta.client.describe_instances(DryRun=True))


def can_describe_ec2_images(ec2: "EC2ServiceResource") -> bool:
    return _is_permitted(lambda: ec2.meta.client.describe_images(DryRun=True))


def can_list_emr_clusters(emr: "EMRClient") -> bool:
    return _is_permitted(lambda: emr.list_clusters(ClusterStates=["RUNNING"]))


def can_describe_emr_cluster(emr: "EMRClient") -> bool:
    return _is_permitted(lambda: emr.describe_cluster(ClusterId=PROBE_EMR_CLUSTER_ID))


def can_list_emr_instance_fleets(emr: "EMRClient") -> bool:
    return _is_permitted(lambda: emr.list_instance_fleets(ClusterId=PROBE_EMR_CLUSTER_ID))


def can_list_emr_instance_groups(emr: "EMRClient") -> bool:
    return _is_permitted(lambda: emr.list_instance_groups(ClusterId=PROBE_EMR_CLUSTER_ID))


def can_list_emr_instances(emr: "EMRClient") -> bool:
    return _is_permitted(lambda: emr.list_instances(ClusterId=PROBE_EMR_CLUSTER_ID))


# endregion - AWS Auth


# region - AWS SSM
def is_aws_ssm_installed() -> bool:
    return which("session-manager-plugin") is not None


def get_aws_ssm_version() -> str | None:
    exe = which("session-manager-plugin")
    if exe is None:
        return None

    result = subprocess.run(  # noqa: S603
        [exe, "--version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    version = result.stdout.strip()
    return version


def get_latest_aws_ssm_version() -> str:
    response = requests.get(AWS_SSM_LATEST_RELEASE_URL, timeout=10)
    response.raise_for_status()
    return response.json()["tag_name"]


# endregion - AWS SSM


# region - opkssh
def is_opkssh_installed() -> bool:
    return which("opkssh") is not None


def get_opkssh_version() -> str | None:
    exe = which("opkssh")
    if exe is None:
        return None

    result = subprocess.run(  # noqa: S603
        [exe, "--version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    version = result.stdout.strip()
    version_match = OPKSSH_VERSION_RE.match(version)
    if not version_match or len(version_match.groups()) != 1:
        raise ValueError(f"Unexpected opkssh version detected: {version}")

    return version_match.group(1)


# endregion - opkssh
