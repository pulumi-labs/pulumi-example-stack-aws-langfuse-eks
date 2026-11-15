"""Deploy the official Langfuse chart against explicitly owned external stores."""
from typing import Final

import pulumi
import pulumi_kubernetes as k8s

import cloud
import settings as s
from pins import CHART_VERSION, WEB_IMAGE_TAG, WORKER_IMAGE_TAG
from stores import stores, store_secret

credentials: Final = k8s.core.v1.Secret("application",
    metadata={"name": "langfuse-secrets", "namespace": s.namespace},
    string_data={"salt": s.salt, "encryption-key": s.encryption_key,
        "session-secret": s.session_secret, "database-password": s.database_password,
        "project-secret-key": s.project_secret_key, "admin-password": s.admin_password},
    opts=s.namespaced_options)
ca: Final = k8s.core.v1.ConfigMap("rds-ca",
    metadata={"name": "rds-ca", "namespace": s.namespace},
    data={"rds-ca.pem": s.ca_path.read_text()}, opts=s.namespaced_options)
service_account: Final = k8s.core.v1.ServiceAccount("storage",
    metadata={"name": "langfuse-storage", "namespace": s.namespace,
        "annotations": {"eks.amazonaws.com/role-arn": cloud.role.arn}},
    automount_service_account_token=False, opts=s.namespaced_options)

release: Final = k8s.helm.v3.Release("langfuse", name="langfuse",
    namespace=s.namespace, chart=str(s.chart_path), version=CHART_VERSION,
    dependency_update=False, timeout=1200, wait_for_jobs=True,
    values={
        "fullnameOverride": "langfuse",
        "langfuse": {
            "salt": {"secretKeyRef": {"name": "langfuse-secrets", "key": "salt"}},
            "encryptionKey": {"secretKeyRef": {"name": "langfuse-secrets", "key": "encryption-key"}},
            "nextauth": {"url": "http://localhost:3000", "secret": {"secretKeyRef": {
                "name": "langfuse-secrets", "key": "session-secret"}}},
            "serviceAccount": {"create": False, "name": "langfuse-storage",
                "automountServiceAccountToken": False},
            "nodeSelector": s.node_selector,
            "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
            "podSecurityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
            "features": {"signUpDisabled": True, "telemetryEnabled": False},
            "resources": {"requests": {"cpu": "500m", "memory": "1Gi"},
                "limits": {"cpu": "2", "memory": "4Gi"}},
            "extraVolumes": [
                {"name": "rds-ca", "configMap": {"name": "rds-ca"}},
                {"name": "aws-identity", "projected": {"sources": [{"serviceAccountToken": {
                    "audience": "sts.amazonaws.com", "expirationSeconds": 3600, "path": "token"}}]}}],
            "extraVolumeMounts": [
                {"name": "rds-ca", "mountPath": "/etc/rds", "readOnly": True},
                {"name": "aws-identity", "mountPath": "/var/run/aws-identity", "readOnly": True}],
            "additionalEnv": [
                {"name": "AWS_ROLE_ARN", "value": cloud.role.arn},
                {"name": "AWS_WEB_IDENTITY_TOKEN_FILE", "value": "/var/run/aws-identity/token"},
                {"name": "AWS_REGION", "value": s.region},
                {"name": "AWS_STS_REGIONAL_ENDPOINTS", "value": "regional"},
                {"name": "AWS_EC2_METADATA_DISABLED", "value": "true"},
                {"name": "LANGFUSE_MIGRATION_V4_WRITE_MODE", "value": "events_only"},
                {"name": "LANGFUSE_MIGRATION_V4_NATIVE_OTEL_BEHAVIOUR", "value": "direct"},
                {"name": "LANGFUSE_MIGRATION_V4_ALLOW_PREVIEW_OPT_IN", "value": "true"},
            ],
            "web": {
                "image": {"tag": WEB_IMAGE_TAG},
                "service": {"type": "ClusterIP"},
                "livenessProbe": {"initialDelaySeconds": 120},
                "pod": {"additionalEnv": [
                    {"name": "LANGFUSE_INIT_ORG_ID", "value": "example-organization"},
                    {"name": "LANGFUSE_INIT_ORG_NAME", "value": "Private tracing"},
                    {"name": "LANGFUSE_INIT_PROJECT_ID", "value": "example-project"},
                    {"name": "LANGFUSE_INIT_PROJECT_NAME", "value": "Python trace ingestion"},
                    {"name": "LANGFUSE_INIT_PROJECT_PUBLIC_KEY", "value": s.project_public_key},
                    {"name": "LANGFUSE_INIT_PROJECT_SECRET_KEY", "valueFrom": {"secretKeyRef": {
                        "name": "langfuse-secrets", "key": "project-secret-key"}}},
                    {"name": "LANGFUSE_INIT_USER_EMAIL", "value": s.config.require("adminEmail")},
                    {"name": "LANGFUSE_INIT_USER_NAME", "value": "Example administrator"},
                    {"name": "LANGFUSE_INIT_USER_PASSWORD", "valueFrom": {"secretKeyRef": {
                        "name": "langfuse-secrets", "key": "admin-password"}}},
                ]}},
            "worker": {"image": {"tag": WORKER_IMAGE_TAG},
                "livenessProbe": {"path": "/", "initialDelaySeconds": 120}},
        },
        "postgresql": {"deploy": False, "host": cloud.database.address, "port": 5432,
            "args": "sslmode=verify-full&sslrootcert=/etc/rds/rds-ca.pem",
            "auth": {"username": "langfuse", "database": "langfuse",
                "existingSecret": "langfuse-secrets",
                "secretKeys": {"userPasswordKey": "database-password"}}},
        "clickhouse": {"deploy": False, "host": "clickhouse",
            "cluster": {"enabled": False},
            "auth": {"username": "langfuse", "existingSecret": "store-secrets",
                "existingSecretKey": "clickhouse-password"}},
        "redis": {"deploy": False, "host": "valkey", "auth": {
            "username": "default", "existingSecret": "store-secrets",
            "existingSecretPasswordKey": "queue-password"}},
        "s3": {"deploy": False, "bucket": cloud.bucket.id, "region": s.region,
            "forcePathStyle": False, "eventUpload": {"prefix": "events/"},
            "mediaUpload": {"enabled": True, "prefix": "media/"},
            "batchExport": {"enabled": True, "prefix": "exports/"}},
    },
    opts=pulumi.ResourceOptions(provider=s.kp, depends_on=[
        credentials, store_secret, ca, service_account, cloud.database,
        cloud.role_policy, cloud.public_block, cloud.ownership, cloud.encryption,
        cloud.cors, cloud.tls_policy, *stores]))

pulumi.export("namespace", s.namespace)
pulumi.export("url", "http://localhost:3000")
pulumi.export("portForward", f"kubectl -n {s.namespace} port-forward service/langfuse-web 3000:3000 --address 127.0.0.1")
pulumi.export("bucketName", cloud.bucket.id)
pulumi.export("databaseIdentifier", cloud.database.identifier)
pulumi.export("projectPublicKey", s.project_public_key)
