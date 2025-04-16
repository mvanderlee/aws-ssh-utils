from dataclasses import dataclass
from typing import TYPE_CHECKING, MutableMapping

import click_spinner
import questionary

if TYPE_CHECKING:
    from mypy_boto3_emr import EMRClient
    from mypy_boto3_emr.type_defs import ClusterSummaryTypeDef, InstanceTypeDef


@dataclass
class IP:
    private: str
    public: str = None


def prompt_for_emr_cluster(
    emr: "EMRClient",
    prompt="Which cluster do you want to connect to?",
    # Either a list of application names, or a dict[name, version]
    applications: dict[str, str] | list[str] = None,
) -> tuple[str, str]:
    """Discovers the available EMR clusters and asks the user which to use.

        Returns the cluster id of the cluster that the user selected.
    """
    # Discover available clusters
    with click_spinner.spinner():
        clusters = get_emr_clusters(emr, applications=applications)

    # Prompt the user
    cluster_name = questionary.select(
        prompt,
        choices=sorted(clusters),
    ).unsafe_ask()
    cluster_id, cluster_name = clusters[cluster_name]

    return cluster_id, cluster_name


def prompt_for_emr_instance_group(
    emr: "EMRClient",
    cluster_id: str, prompt="Which instance group do you want to connect to?"
) -> tuple[list[IP], str]:
    """Discovers the available instance groups for the selected EMR cluster.
        Note that instance group and instance fleet are both treated the same here.

        Returns the list of instances in the selected group, as well as the group name
    """
    # Discover the available instances grouped by groupname
    with click_spinner.spinner():
        grouped_instances = get_emr_instance_ips(emr, cluster_id)

    # Prompt the user
    group_name = questionary.select(
        prompt,
        choices=sorted(grouped_instances),
    ).unsafe_ask()

    instances = grouped_instances[group_name]
    return instances, group_name


def does_cluster_have_applications(
    emr: "EMRClient",
    cluster_id: str,
    applications: dict[str, str] | list[str] = None,
) -> bool:
    cluster_details = emr.describe_cluster(ClusterId=cluster_id).get("Cluster")

    if not cluster_details:
        return False

    cluster_applications = {a["Name"]: a["Version"] for a in cluster_details["Applications"]}

    if isinstance(applications, MutableMapping):
        # If a map, check that all the required applications are in the cluster and at the version
        return all(v == cluster_applications.get(k) for k, v in applications.items())
    else:
        return all(k in cluster_applications for k in applications)


def get_emr_clusters(
    emr: "EMRClient",
    states: list[str] = None,
    # Either a list of application names, or a dict[name, version]
    applications: dict[str, str] | list[str] = None,
) -> dict[str, tuple[str, str]]:
    """Discover the available EMR cluster

        Returns a dict where the key is the cluster "Name - ID - CreatedDT" and the value is the ID.
    """
    if states is None:
        states = ["RUNNING", "WAITING"]

    clusters = emr.list_clusters(
        ClusterStates=states,
    ).get("Clusters", [])

    if applications:
        clusters = [c for c in clusters if does_cluster_have_applications(emr, c["Id"], applications=applications)]

    def get_display_name(c: "ClusterSummaryTypeDef") -> str:
        created = c["Status"]["Timeline"]["CreationDateTime"].strftime("%Y-%m-%d %H:%M")
        return f"{c['Name']} - {c['Id']} - {created}"

    return {f"{get_display_name(c)}": (c["Id"], c["Name"]) for c in clusters}


def get_emr_groups(
    emr: "EMRClient",
    cluster_id: str,
):
    """Discover the EMR groups.

        Returns the groups and the id_name to use for list_instances
    """
    cluster_details = emr.describe_cluster(ClusterId=cluster_id)
    instance_collection_type = cluster_details["Cluster"].get("InstanceCollectionType")

    if instance_collection_type == "INSTANCE_FLEET":
        id_name = "InstanceFleetId"
        groups = emr.list_instance_fleets(ClusterId=cluster_id)
        groups = groups["InstanceFleets"]

    elif instance_collection_type == "INSTANCE_GROUP":
        id_name = "InstanceGroupId"
        groups = emr.list_instance_groups(ClusterId=cluster_id)
        groups = groups["InstanceGroups"]

    return groups, id_name


def get_emr_instances(
    emr: "EMRClient",
    cluster_id: str,
) -> dict[str, list["InstanceTypeDef"]]:
    """Discover the running EMR instances per instance group"""
    groups, id_name = get_emr_groups(emr, cluster_id)

    groups = {g["Id"]: g["Name"] for g in groups}
    grouped_instances = {g: [] for g in groups.values()}

    instances = emr.list_instances(ClusterId=cluster_id, InstanceStates=["RUNNING"])
    for instance in instances["Instances"]:
        grouped_instances[groups[instance[id_name]]].append(instance)

    return grouped_instances


def get_emr_instance_ips(
    emr: "EMRClient",
    cluster_id: str,
) -> dict[str, list[IP]]:
    '''Returns a dict of IPs per group'''
    return {
        k: [
            IP(x["PrivateIpAddress"], x.get("PublicIpAddress"))
            for x in v
        ]
        for k, v in get_emr_instances(emr, cluster_id).items()
    }
