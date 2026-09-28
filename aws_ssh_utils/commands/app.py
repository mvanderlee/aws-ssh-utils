import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from itertools import count
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

import boto3
import click
from botocore.exceptions import BotoCoreError, ClientError
from loguru import logger
from rich.console import Console
from rich.markup import escape
from rich.spinner import Spinner
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.logging import TextualHandler
from textual.widgets import Footer, Input, OptionList, RichLog, Static, TabbedContent, TabPane, Tree
from textual.widgets.option_list import Option
from textual.widgets.tree import TreeNode

from aws_ssh_utils.cli_utils import detect_environment, detect_scrollback, render_attempts, scrollback_option
from aws_ssh_utils.connection import Attempt, Environment, Target, connect, open_shell_channel
from aws_ssh_utils.ec2_utils import ec2_name, ec2_target, get_running_ec2_instances
from aws_ssh_utils.emr_utils import emr_target, get_emr_clusters, get_emr_instances, group_role, group_sort_key
from aws_ssh_utils.terminal import ShellStatus, Terminal

if TYPE_CHECKING:
    from mypy_boto3_ec2 import EC2Client
    from mypy_boto3_ec2.type_defs import InstanceTypeDef as EC2InstanceTypeDef
    from mypy_boto3_emr import EMRClient
    from mypy_boto3_emr.type_defs import InstanceTypeDef as EMRInstanceTypeDef

Status = Literal['connecting', 'waiting', 'connected', 'failed']
STATUS_ICONS: dict[Status, Text] = {
    'waiting': Text('⚠', style='yellow'),
    'connected': Text('●', style='green'),
    'failed': Text('●', style='red'),
}
ANIMATION_INTERVAL = 1 / 12


@click.command("app")
@click.option('-p', '--profile', default=None, help='Which AWS profile to use')
@click.option('-r', '--region', default=None, help='Which AWS region to use')
@scrollback_option
def app(profile: str | None = None, region: str | None = None, scrollback: int | None = None, **kwargs: Any):
    """Browse EC2 instances and EMR clusters, and open SSH shells in tabs."""
    try:
        session = boto3.Session(profile_name=profile, region_name=region)
        session.client('sts').get_caller_identity()
    except (BotoCoreError, ClientError) as e:
        Console().print(f"[red]✗[/] {escape(str(e))}")
        sys.exit(1)

    # loguru holds on to the real stdout, which would draw over the TUI. View logs with `textual console`.
    logger.remove()
    logger.add(TextualHandler(), format="{message}")

    env = detect_environment(profile, session.region_name)
    scrollback = detect_scrollback() if scrollback is None else scrollback
    SSHApp(session.client('ec2'), session.client('emr'), env, scrollback).run()


def muted_label(name: str, instance_id: str) -> Text:
    return Text.assemble(name, (f" - {instance_id}", 'dim'))


@dataclass(frozen=True)
class Cluster:
    id: str
    name: str


@dataclass(frozen=True)
class EMRNode:
    cluster_id: str
    group_name: str
    instance: "EMRInstanceTypeDef"


class BouncingBall(Static):
    def on_mount(self):
        spinner = Spinner('bouncingBall')
        self.set_interval(ANIMATION_INTERVAL, lambda: self.update(spinner.render(time.monotonic())))


class ShellPane(TabPane):
    """Connects in the background, then swaps its log for a Terminal."""

    def __init__(self, title: str, resolve: Callable[[], Target], env: Environment, scrollback: int, **kwargs: Any):
        super().__init__(title, **kwargs)
        self.title_ = title
        self.resolve = resolve
        self.env = env
        self.scrollback = scrollback
        self.status: Status = 'connecting'
        self.spinner = Spinner('dots')
        self.attempts: list[Attempt] = []

    def compose(self) -> ComposeResult:
        yield Static(classes='attempts')
        yield RichLog(wrap=True, markup=True)

    def on_mount(self):
        self.animation = self.set_interval(ANIMATION_INTERVAL, self.update_label)
        self.connect()

    def update_label(self):
        icon = cast(Text, self.spinner.render(time.monotonic())) if self.status == 'connecting' else STATUS_ICONS[self.status]
        self.query_ancestor(TabbedContent).get_tab(self).label = Text.assemble(icon, ' ', self.title_)
        self.query_one('.attempts', Static).update(render_attempts(self.attempts))

    def set_attempts(self, attempts: list[Attempt]):
        self.attempts = attempts
        self.update_label()

    def set_status(self, status: Status):
        self.status = status
        if status != 'connecting':
            self.animation.stop()
        self.update_label()

    def write(self, markup: str):
        self.query_one(RichLog).write(markup)

    @work(thread=True, exit_on_error=False)
    def connect(self):
        def write(line: str):
            self.app.call_from_thread(self.write, escape(line))

        try:
            result = connect(
                self.resolve(),
                self.env,
                on_output=write,
                on_waiting=lambda: self.app.call_from_thread(self.set_status, 'waiting'),
                on_progress=lambda attempts: self.app.call_from_thread(self.set_attempts, attempts),
            )
            if result is None:
                self.app.call_from_thread(self.set_status, 'failed')
                return

            channel = open_shell_channel(result.client)
        except Exception as e:
            logger.exception("Connection failed")
            self.app.call_from_thread(self.write, f"[red]✗ {escape(str(e) or type(e).__name__)}[/]")
            self.app.call_from_thread(self.set_status, 'failed')
            return

        self.app.call_from_thread(self.attach, result.via, Terminal(result.client, channel, self.scrollback))

    async def attach(self, via: str, terminal: Terminal):
        await self.query_one(RichLog).remove()
        await self.query_one('.attempts').remove()
        await self.mount(ShellStatus(via), terminal)
        self.set_status('connected')

    def on_terminal_closed(self):
        self.query_ancestor(TabbedContent).remove_pane(self.id or '')


class SSHApp(App[None]):
    """The textual app"""

    TITLE = "AWS SSH"
    CSS = """
    #sidebar { width: 60; }
    #shells { width: 1fr; }
    Footer { background: $surface; }
    TabbedContent, TabbedContent > ContentSwitcher, TabPane { height: 1fr; }
    #ec2-list, #emr-tree { height: 1fr; }
    #emr-tree {
        border: tall $border-blurred;
        padding: 0 1;
        background: $surface;
        &:focus { border: tall $border; }
    }
    .error { color: $error; }
    """
    # Its priority ctrl+p binding would steal shell history navigation from terminals.
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding('ctrl+q', 'quit', 'Quit', priority=True),
        Binding('f12', 'toggle_sidebar', 'Toggle sidebar', priority=True),
        Binding('ctrl+w', 'close_tab', 'Close tab'),
    ]

    def __init__(self, ec2: "EC2Client", emr: "EMRClient", env: Environment, scrollback: int):
        super().__init__()
        self.ec2 = ec2
        self.emr = emr
        self.env = env
        self.scrollback = scrollback
        self.ec2_instances: list[EC2InstanceTypeDef] = []
        self.clusters: list[Cluster] = []
        self.cluster_instances: dict[str, dict[str, list[EMRInstanceTypeDef]]] = {}
        self.emr_loading = 0
        self.pane_ids = count()

    def compose(self) -> ComposeResult:
        with Horizontal():
            with TabbedContent(id='sidebar'):
                with TabPane('EC2'):
                    yield Input(placeholder='Filter by name', id='ec2-filter')
                    yield BouncingBall(id='ec2-loading')
                    yield Static(id='ec2-error', classes='error')
                    yield OptionList(id='ec2-list')
                with TabPane('EMR'):
                    yield Input(placeholder='Filter by name', id='emr-filter')
                    yield BouncingBall(id='emr-loading')
                    yield Static(id='emr-error', classes='error')
                    yield Tree('Clusters', id='emr-tree')
            yield TabbedContent(id='shells')
        yield Footer()

    def on_mount(self):
        self.query_one('#ec2-error').display = False
        self.query_one('#emr-error').display = False
        self.query_one('#emr-tree', Tree).show_root = False
        self.load_ec2()
        self.load_emr()

    def show_error(self, prefix: str, error: Exception):
        self.query_one(f'#{prefix}-loading').display = False
        error_widget = self.query_one(f'#{prefix}-error', Static)
        error_widget.update(Text(str(error)))
        error_widget.display = True

    def set_emr_loading(self, delta: int):
        self.emr_loading += delta
        self.query_one('#emr-loading').display = self.emr_loading > 0

    def on_input_changed(self, event: Input.Changed):
        if event.input.id == 'ec2-filter':
            self.filter_ec2()
        else:
            self.filter_emr()

    def action_toggle_sidebar(self):
        sidebar = self.query_one('#sidebar')
        sidebar.display = not sidebar.display
        if not sidebar.display and self.focused and sidebar in self.focused.ancestors_with_self:
            pane = self.query_one('#shells', TabbedContent).active_pane
            self.set_focus(pane.query(Terminal).first() if pane and pane.query(Terminal) else None)

    def action_close_tab(self):
        shells = self.query_one('#shells', TabbedContent)
        if shells.active:
            shells.remove_pane(shells.active)

    def open_shell(self, title: str, resolve: Callable[[], Target]):
        shells = self.query_one('#shells', TabbedContent)
        pane = ShellPane(title, resolve, self.env, self.scrollback, id=f'shell-{next(self.pane_ids)}')
        shells.add_pane(pane)
        shells.active = pane.id or ''

    # region - EC2
    @work(thread=True, exit_on_error=False)
    def load_ec2(self):
        try:
            instances = get_running_ec2_instances(self.ec2)
        except (BotoCoreError, ClientError) as e:
            self.call_from_thread(self.show_error, 'ec2', e)
            return
        self.call_from_thread(self.set_ec2_instances, sorted(instances, key=lambda i: ec2_name(i).lower()))

    def set_ec2_instances(self, instances: list["EC2InstanceTypeDef"]):
        self.ec2_instances = instances
        self.query_one('#ec2-loading').display = False
        self.filter_ec2()

    def filter_ec2(self):
        name_filter = self.query_one('#ec2-filter', Input).value.lower()
        option_list = self.query_one('#ec2-list', OptionList)
        option_list.clear_options()
        option_list.add_options(
            Option(muted_label(ec2_name(i), i.get('InstanceId', '')), id=i.get('InstanceId', ''))
            for i in self.ec2_instances
            if name_filter in ec2_name(i).lower()
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        instance = next(i for i in self.ec2_instances if i.get('InstanceId', '') == event.option_id)
        self.open_shell(ec2_name(instance), lambda: ec2_target(self.ec2, instance))

    # endregion - EC2

    # region - EMR
    @work(thread=True, exit_on_error=False)
    def load_emr(self):
        self.call_from_thread(self.set_emr_loading, 1)
        try:
            clusters = [Cluster(cluster_id, name) for cluster_id, name in get_emr_clusters(self.emr).values()]
        except (BotoCoreError, ClientError) as e:
            self.call_from_thread(self.show_error, 'emr', e)
            return
        finally:
            self.call_from_thread(self.set_emr_loading, -1)
        self.call_from_thread(self.set_clusters, sorted(clusters, key=lambda c: c.name.lower()))

    def set_clusters(self, clusters: list[Cluster]):
        self.clusters = clusters
        self.filter_emr()

    def filter_emr(self):
        name_filter = self.query_one('#emr-filter', Input).value.lower()
        tree = self.query_one('#emr-tree', Tree)
        tree.clear()
        for cluster in self.clusters:
            if name_filter in cluster.name.lower():
                node = tree.root.add(muted_label(cluster.name, cluster.id), data=cluster)
                if cluster.id in self.cluster_instances:
                    self.add_groups(node, cluster.id)

    def on_tree_node_expanded(self, event: Tree.NodeExpanded[Any]):
        cluster = event.node.data
        if isinstance(cluster, Cluster) and cluster.id not in self.cluster_instances and not event.node.children:
            self.load_cluster_instances(cluster.id)

    @work(thread=True, exit_on_error=False)
    def load_cluster_instances(self, cluster_id: str):
        self.call_from_thread(self.set_emr_loading, 1)
        try:
            groups = get_emr_instances(self.emr, cluster_id)
        except (BotoCoreError, ClientError, ValueError) as e:
            self.call_from_thread(self.show_cluster_error, cluster_id, e)
            return
        finally:
            self.call_from_thread(self.set_emr_loading, -1)
        self.call_from_thread(self.set_cluster_instances, cluster_id, groups)

    def cluster_node(self, cluster_id: str) -> TreeNode[Any] | None:
        tree = self.query_one('#emr-tree', Tree)
        return next((n for n in tree.root.children if isinstance(n.data, Cluster) and n.data.id == cluster_id), None)

    def show_cluster_error(self, cluster_id: str, error: Exception):
        if node := self.cluster_node(cluster_id):
            node.add_leaf(Text(str(error), style='red'))

    def set_cluster_instances(self, cluster_id: str, groups: dict[str, list["EMRInstanceTypeDef"]]):
        self.cluster_instances[cluster_id] = groups
        if node := self.cluster_node(cluster_id):
            self.add_groups(node, cluster_id)

    def add_groups(self, node: TreeNode[Any], cluster_id: str):
        groups = self.cluster_instances[cluster_id]
        for group_name in sorted(groups, key=group_sort_key):
            group = node.add(group_name, expand=True)
            for instance in sorted(groups[group_name], key=lambda i: i.get('PrivateIpAddress', '')):
                label = muted_label(instance.get('PrivateIpAddress', ''), instance.get('Ec2InstanceId', ''))
                group.add_leaf(label, data=EMRNode(cluster_id, group_name, instance))

    def on_tree_node_selected(self, event: Tree.NodeSelected[Any]):
        selected = event.node.data
        if isinstance(selected, EMRNode):
            title = f"{group_role(selected.group_name)} {selected.instance.get('PrivateIpAddress', '')}"
            self.open_shell(
                title,
                lambda: emr_target(
                    self.emr,
                    selected.cluster_id,
                    selected.instance.get('Ec2InstanceId', ''),
                    selected.instance.get('PrivateIpAddress', ''),
                ),
            )

    # endregion - EMR
