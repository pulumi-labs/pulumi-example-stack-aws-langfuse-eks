"""Single-node development stores with persistent volumes and private services."""
from typing import Final

import pulumi
import pulumi_kubernetes as k8s

import settings as s
from pins import CLICKHOUSE_IMAGE, VALKEY_IMAGE

store_secret: Final = k8s.core.v1.Secret("stores",
    metadata={"name": "store-secrets", "namespace": s.namespace},
    string_data={"clickhouse-password": s.clickhouse_password, "queue-password": s.queue_password},
    opts=s.namespaced_options)
clickhouse: Final = k8s.core.v1.ContainerArgs(name="clickhouse", image=CLICKHOUSE_IMAGE,
    image_pull_policy="IfNotPresent",
    env=[
        {"name": "CLICKHOUSE_USER", "value": "langfuse"},
        {"name": "CLICKHOUSE_DB", "value": "default"},
        {"name": "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT", "value": "1"},
        {"name": "CLICKHOUSE_PASSWORD", "value_from": {"secret_key_ref": {
            "name": "store-secrets", "key": "clickhouse-password"}}},
        {"name": "TZ", "value": "UTC"},
    ],
    ports=[{"container_port": 8123}, {"container_port": 9000}],
    volume_mounts=[{"name": "data", "mount_path": "/var/lib/clickhouse"}],
    resources={"requests": {"cpu": "1", "memory": "2Gi"},
        "limits": {"cpu": "2", "memory": "4Gi"}},
    readiness_probe={"http_get": {"path": "/ping", "port": 8123}, "timeout_seconds": 5},
    security_context={"allow_privilege_escalation": False, "capabilities": {"drop": ["ALL"]}})
valkey: Final = k8s.core.v1.ContainerArgs(name="valkey", image=VALKEY_IMAGE,
    image_pull_policy="IfNotPresent",
    env=[
        {"name": "VALKEY_PASSWORD", "value_from": {"secret_key_ref": {
            "name": "store-secrets", "key": "queue-password"}}},
    ],
    ports=[{"container_port": 6379}],
    volume_mounts=[{"name": "data", "mount_path": "/data"}],
    resources={"requests": {"cpu": "100m", "memory": "128Mi"},
        "limits": {"cpu": "500m", "memory": "512Mi"}},
    readiness_probe={"exec_": {"command": ["sh", "-ec",
        'test "$(REDISCLI_AUTH="$VALKEY_PASSWORD" valkey-cli ping)" = PONG']},
        "timeout_seconds": 5},
    command=["sh", "-ec"], args=[
            'exec docker-entrypoint.sh valkey-server --appendonly yes '
            '--maxmemory 384mb --maxmemory-policy noeviction --requirepass "$VALKEY_PASSWORD"'],
    security_context={"allow_privilege_escalation": False, "capabilities": {"drop": ["ALL"]}})
stores: list[pulumi.Resource] = []
for name, container, uid, size, numbers in [
    ("clickhouse", clickhouse, 101, "20Gi", [8123, 9000]),
    ("valkey", valkey, 999, "5Gi", [6379]),
]:
    labels = {"app.kubernetes.io/name": name}
    service = k8s.core.v1.Service(name, metadata={"name": name, "namespace": s.namespace},
        spec={"type": "ClusterIP", "selector": labels,
            "ports": [{"name": "tcp-" + str(port), "port": port} for port in numbers]},
        opts=s.namespaced_options)
    pvc = k8s.core.v1.PersistentVolumeClaim(name,
        metadata={"name": name + "-data", "namespace": s.namespace},
        spec={"access_modes": ["ReadWriteOnce"], "storage_class_name": s.storage_class,
            "resources": {"requests": {"storage": size}}},
        opts=pulumi.ResourceOptions(provider=s.kp, depends_on=[s.ns], protect=not s.disposable))
    workload = k8s.apps.v1.StatefulSet(name,
        metadata={"name": name, "namespace": s.namespace},
        spec={"replicas": 1, "service_name": name, "selector": {"match_labels": labels},
            "template": {"metadata": {"labels": labels}, "spec": {
                "node_selector": s.node_selector, "automount_service_account_token": False,
                "security_context": {"run_as_user": uid, "run_as_group": uid, "fs_group": uid,
                    "seccomp_profile": {"type": "RuntimeDefault"}},
                "containers": [container], "volumes": [{"name": "data",
                    "persistent_volume_claim": {"claim_name": name + "-data"}}]}}},
        opts=pulumi.ResourceOptions(provider=s.kp, depends_on=[store_secret, pvc, service]))
    policy = k8s.networking.v1.NetworkPolicy(name,
        metadata={"name": name, "namespace": s.namespace},
        spec={"pod_selector": {"match_labels": labels}, "policy_types": ["Ingress"],
            "ingress": [{"from_": [{"pod_selector": {"match_labels": {
                "app.kubernetes.io/instance": "langfuse"}}}],
                "ports": [{"protocol": "TCP", "port": port} for port in numbers]}]},
        opts=s.namespaced_options)
    stores.extend([workload, policy])
