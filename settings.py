"""Parse the deployment boundary before any resources are registered."""
import hashlib
import json
from pathlib import Path
import re
from typing import Final

import pulumi
import pulumi_aws as aws
import pulumi_kubernetes as k8s

from pins import CHART_SHA256, RDS_CA_SHA256

ROOT: Final = Path(__file__).resolve().parent
config: Final = pulumi.Config()
account: Final = config.require("accountId")
region: Final = "us-east-2"
cluster_name: Final = config.require("clusterName")
vpc: Final = config.require("vpcId")
subnets: Final = config.require("privateSubnetIds").split(",")
node_sg: Final = config.require("nodeSecurityGroupId")
storage_class: Final = config.require("storageClass")
node_group: Final = config.require("nodeGroupName")
resource_prefix: Final = config.get("resourcePrefix") or "langfuse-" + pulumi.get_stack()
namespace: Final = resource_prefix
disposable: Final = config.get_bool("disposable") or False
chart_path: Final = ROOT / ".artifacts/langfuse-2.2.4.tgz"
ca_path: Final = ROOT / "rds-ca.pem"

if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", pulumi.get_stack()):
    raise pulumi.RunError("Stack name must be a lowercase DNS label of at most 31 characters")
if not re.fullmatch(r"[a-z][a-z0-9-]{0,29}[a-z0-9]", resource_prefix) or "--" in resource_prefix:
    raise pulumi.RunError("resourcePrefix requires a lowercase DNS label of 2..31 characters")
if not re.fullmatch(r"\d{12}", account):
    raise pulumi.RunError("accountId must be an exact 12-digit AWS account")
if len(subnets) != 2 or len(set(subnets)) != 2 or any(
    not re.fullmatch(r"subnet-[0-9a-f]+", subnet) for subnet in subnets
):
    raise pulumi.RunError("privateSubnetIds requires two distinct comma-separated subnet IDs")
if hashlib.sha256(chart_path.read_bytes()).hexdigest() != CHART_SHA256:
    raise pulumi.RunError("Chart bytes differ from the reviewed release")
if hashlib.sha256(ca_path.read_bytes()).hexdigest() != RDS_CA_SHA256:
    raise pulumi.RunError("RDS CA bytes differ from the reviewed regional bundle")


def secret(key: str, pattern: str = r"[A-Za-z0-9_-]{32,128}") -> pulumi.Output[str]:
    """Accept URL-safe high-entropy secrets without logging their values."""
    def parse(value: str) -> str:
        if not re.fullmatch(pattern, value):
            raise pulumi.RunError(f"{key} does not match its documented secret format")
        return value
    return config.require_secret(key).apply(parse)


database_password: Final = secret("databasePassword")
queue_password: Final = secret("queuePassword")
clickhouse_password: Final = secret("clickhousePassword")
admin_password: Final = secret("adminPassword")
project_public_key: Final = config.require("projectPublicKey")
project_secret_key: Final = secret("projectSecretKey", r"sk-lf-[A-Za-z0-9_-]{32,128}")
encryption_key: Final = secret("encryptionKey", r"[0-9a-f]{64}")
salt: Final = secret("salt")
session_secret: Final = secret("sessionSecret")

if not re.fullmatch(r"pk-lf-[A-Za-z0-9_-]{32,128}", project_public_key):
    raise pulumi.RunError("projectPublicKey requires pk-lf- followed by 32..128 URL-safe characters")


awsp: Final = aws.Provider("aws", region=region, allowed_account_ids=[account],
    default_tags={"tags": {"example": "langfuse", "stack": pulumi.get_stack(),
        "resource-prefix": resource_prefix, "pulumi-stack": pulumi.get_stack(),
        "pulumi-project": pulumi.get_project()}})
ao: Final = pulumi.ResourceOptions(provider=awsp)
data_options: Final = pulumi.ResourceOptions(provider=awsp, protect=not disposable)
invoke_options: Final = pulumi.InvokeOptions(provider=awsp)
cluster: Final = aws.eks.get_cluster(name=cluster_name, opts=invoke_options)
issuer: Final = cluster.identities[0].oidcs[0].issuer.removeprefix("https://")
if cluster.vpc_config.vpc_id != vpc:
    raise pulumi.RunError("Selected cluster must belong to the configured VPC")
subnet_metadata: Final = [aws.ec2.get_subnet(id=s, opts=invoke_options) for s in subnets]
if any(s.vpc_id != vpc or s.map_public_ip_on_launch for s in subnet_metadata):
    raise pulumi.RunError("RDS subnets must be private and belong to the cluster VPC")
if len({s.availability_zone for s in subnet_metadata}) != 2:
    raise pulumi.RunError("RDS subnets must occupy two distinct availability zones")

# Use the approved runner's short-lived AWS identity through the native EKS exec plugin.
kubeconfig: Final = json.dumps({
    "apiVersion": "v1", "kind": "Config", "current-context": "langfuse",
    "clusters": [{"name": cluster_name, "cluster": {"server": cluster.endpoint,
        "certificate-authority-data": cluster.certificate_authorities[0].data}}],
    "contexts": [{"name": "langfuse", "context": {"cluster": cluster_name, "user": "runner"}}],
    "users": [{"name": "runner", "user": {"exec": {
        "apiVersion": "client.authentication.k8s.io/v1beta1", "command": "aws",
        "args": ["eks", "get-token", "--cluster-name", cluster_name, "--region", region],
        "interactiveMode": "Never"}}}],
})
kp: Final = k8s.Provider("eks", kubeconfig=kubeconfig, namespace=namespace)
ko: Final = pulumi.ResourceOptions(provider=kp)
ns: Final = k8s.core.v1.Namespace("langfuse", metadata={"name": namespace}, opts=ko)
namespaced_options: Final = pulumi.ResourceOptions(provider=kp, depends_on=[ns])
node_selector: Final = {"kubernetes.io/os": "linux", "kubernetes.io/arch": "amd64",
    "eks.amazonaws.com/nodegroup": node_group}
