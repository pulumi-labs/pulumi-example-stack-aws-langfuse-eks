# Private Langfuse tracing on AWS EKS with Python Pulumi

Deploy Langfuse web and worker on an existing EKS cluster, with RDS PostgreSQL,
S3, and persistent single-node ClickHouse and Valkey. The included Python
application emits a deterministic two-span trace, attaches a score, and
retrieves the exact stored result through authenticated v4 APIs.

Validated on November 15, 2026 in a disposable private AWS deployment:
refreshed preview, deployment, nine fixture tests, exact two-span ingestion and
retrieval, numeric score 1, six denied authentication requests, and S3 event
uploads passed. Teardown deleted 29 resources; independent inventory confirmed
no owned database, backups, snapshots, bucket, IAM role/policies, security group,
database subnet/parameter groups, namespace, claims, persistent volumes or EBS
volumes remained. Shared infrastructure was preserved. HA, scaling and recovery
remain untested.

## What this creates

- One private namespace with the official Langfuse Helm release.
- One encrypted private RDS PostgreSQL 15.18 database, 20Gi gp3, db.t4g.small.
- One private encrypted S3 bucket for events, media and exports, accessed through
  a bucket-scoped IAM role and projected EKS OIDC token.
- One 20Gi ClickHouse PVC and one 5Gi Valkey PVC, with private services.
- A real administrator, organization, project and API keys through Langfuse's
  supported headless startup initialization.

The chart uses its external-store interface, so this example needs no
ClickHouse operator, Keeper, cert-manager, Tailscale, ingress controller or CI.
Single-container ClickHouse is upstream's community development option.
There is no HA, backup/restore implementation, performance or production
readiness claim. Web and worker have separate Deployments; independent scaling
is available in the chart but has not been tested here.

No EKS cluster, runner, VPC, node group, OIDC provider or CSI driver is created.
All application services use ClusterIP. Operator access uses an authenticated
Kubernetes port-forward to loopback and then Langfuse login/API authentication.

## Prerequisites

Use an existing AWS account and EKS cluster in `us-east-2`, with Linux AMD64
nodes, a selected managed node group, normal VPC CNI networking, an existing
EKS IAM OIDC provider, and EBS CSI storage with a Delete reclaim policy.
Supply two private RDS subnets in distinct availability zones, the cluster VPC,
and the security group used by the selected nodes. Pods using security groups
for pods require a corresponding deliberate RDS rule change.

The approved execution machine must reach the private Kubernetes API and AWS
APIs, registries and DNS. Its existing short-lived provisioning identity needs
the AWS resource and Kubernetes namespace privileges represented by this
program. It must have reviewed Python 3.12, uv, AWS CLI, kubectl and Pulumi
3.273.0, plus AWS provider 7.52.1 and Kubernetes provider 4.34.2.
Prefer preinstalled, scanned tools; this source does not install tools.

Scan the exact Python wheels, provider binaries, chart archive, image manifests
and all layers before executing them. Use an approved sandbox for installs,
preview and workload checks. Do not run downloaded code or this program on an
unisolated workstation. A vulnerability audit is not a malware scan.

Clone the private repository with an account that has access:

```sh
git clone https://github.com/pulumi-labs/pulumi-example-stack-aws-langfuse-eks.git
cd pulumi-example-stack-aws-langfuse-eks
```

Use an approved execution environment for the commands below.

## Prepare the reviewed artifacts

In the approved execution environment, acquire the chart as inert data:

```sh
mkdir -p .artifacts
curl --fail --location \
  https://github.com/langfuse/langfuse-k8s/releases/download/langfuse-2.2.4/langfuse-2.2.4.tgz \
  --output .artifacts/langfuse-2.2.4.tgz
printf '%s  %s\n' \
  29e657662e210c0914bfce914d33b61101b890f2d2cda0e871ca8fcaac6fb3f6 \
  .artifacts/langfuse-2.2.4.tgz | sha256sum -c -
```

After complete malware coverage of that archive and the exact wheelhouse and
plugins, create the environment using the already installed Python:

```sh
: "${REVIEWED_WHEELHOUSE:?Set the complete scanned wheelhouse directory}"
uv venv --python 3.12 --no-python-downloads
uv pip sync --python .venv/bin/python --no-index \
  --find-links "$REVIEWED_WHEELHOUSE" --require-hashes \
  --only-binary :all: requirements.lock
```

The runtime lock reuses the reviewed Pulumi/AWS/Kubernetes closure and adds a
small pinned Pydantic response parser. Hashes cover CPython 3.12 Linux ARM64 and
AMD64 wheels where native wheels are required. Application nodes are AMD64.
The exact runtime image manifests are in `pins.py`. Do not replace them with
floating tags. New or regenerated runnable outputs require scanning before use.

## Configure

Use a secrets backend you control and keep its encryption key outside the
repository. Set the following shell variables from the verified platform
inventory, then configure the stack:

```sh
: "${ACCOUNT_ID:?}" "${CLUSTER_NAME:?}" "${VPC_ID:?}"
: "${PRIVATE_SUBNET_IDS:?Comma-separated IDs of two private subnets}"
: "${NODE_SECURITY_GROUP_ID:?}" "${NODE_GROUP_NAME:?}" "${STORAGE_CLASS:?}"
pulumi stack init dev
pulumi config set accountId "$ACCOUNT_ID"
pulumi config set clusterName "$CLUSTER_NAME"
pulumi config set vpcId "$VPC_ID"
pulumi config set privateSubnetIds "$PRIVATE_SUBNET_IDS"
pulumi config set nodeSecurityGroupId "$NODE_SECURITY_GROUP_ID"
pulumi config set nodeGroupName "$NODE_GROUP_NAME"
pulumi config set storageClass "$STORAGE_CLASS"
pulumi config set adminEmail
```

`adminEmail` prompts for the initial administrator email. The default AWS
resource prefix and namespace are `langfuse-<stack>`. To scope a trial identity,
set `resourcePrefix` to a unique lowercase DNS label, such as `ae4-langfuse`.
Database, subnet/parameter/security groups, IAM role and bucket prefix all use
it. Provider tags include `pulumi-project`, `pulumi-stack` and `resource-prefix`.
An optional `permissionsBoundaryArn` applies only to the application IAM role.
Neither trial prefixes nor a trial permissions boundary are product requirements.

Generate and store new secrets without printing them:

```sh
for key in databasePassword queuePassword clickhousePassword adminPassword salt sessionSecret encryptionKey
do
  openssl rand -hex 32 | pulumi config set --secret "$key"
done
pulumi config set projectPublicKey "pk-lf-$(openssl rand -hex 32)"
printf 'sk-lf-%s' "$(openssl rand -hex 32)" | pulumi config set --secret projectSecretKey
```

Ordinary secrets accept 32..128 URL-safe characters. The encryption key must
be 64 lowercase hexadecimal characters; project keys need `pk-lf-` / `sk-lf-`
prefixes followed by 32..128 URL-safe characters. Never enable shell tracing
while handling secrets. Store the generated administrator password securely
outside source control before using it.

Startup initializes the project and account without requiring the sample
application. Headless initialization is creation-only: changing its environment
variables does not rotate existing Langfuse API keys or administrator passwords.
Rotate those in product settings. Changes to referenced Kubernetes Secrets also
need an attended rollout of their consumers.

## Deploy and use

Inspect the exact saved preview before applying:

```sh
pulumi preview --save-plan deployment.plan
pulumi up --plan deployment.plan
kubectl -n "$(pulumi stack output namespace)" \
  port-forward service/langfuse-web 3000:3000 --address 127.0.0.1
```

Keep that port-forward open in its terminal. Open `http://localhost:3000` and log
in with the configured administrator. Signup is disabled. Browser media uploads
use S3 presigned URLs with CORS restricted to this local origin. PostgreSQL uses
the bundled regional CA and `verify-full`; S3 uses HTTPS. Cluster-private
ClickHouse/Valkey connections are password-authenticated plaintext.

In another terminal in the same reviewed directory:

```sh
export LANGFUSE_PUBLIC_KEY="$(pulumi config get projectPublicKey)"
export LANGFUSE_SECRET_KEY="$(pulumi config get projectSecretKey)"
export TRACE_RUN_ID="$(openssl rand -hex 16)"
.venv/bin/python trace_demo.py
unset LANGFUSE_SECRET_KEY LANGFUSE_PUBLIC_KEY TRACE_RUN_ID
```

The client always connects to `127.0.0.1:3000`, does not use proxy environment
variables or follow redirects, and makes no LLM call. A fresh run token produces
deterministic trace/span/score IDs. It requires an empty trace ID before sending
and waits up to 180 seconds for exact persisted output.

Success prints `status=passed` as JSON, the public IDs, score `1`, and six denied
requests. The two spans must be `answer` and its child `retrieve`, with the
expected input/output, environment and trace name. It verifies the v3 score
subject identifies the same trace. Missing and wrong project credentials must
receive HTTP 401 for observation reads, score reads and OTLP writes.

This proves more than HTTP acceptance: the worker must process the queue and
write the result before retrieval succeeds. OTLP partial acceptance fails the
check. v4 observations and v3 scores are used because older trace/score readers
are unavailable in events-only mode.

## Troubleshooting and limits

Pending pods usually mean the selected node group lacks capacity, the AMD64
selector differs, or the CSI class cannot bind. Requests total roughly 2.1 CPU
and 4.125Gi RAM before platform overhead and rollout surge; limits total 6.5 CPU
and 12.5Gi. One m6i.xlarge is a small trial pool, not a capacity guarantee.

Database connection failures require checking the actual node/pod SG path,
subnet routing, native startup logs and the unchanged RDS CA. Never disable
certificate verification to get past a startup failure. Migration logs can
contain operational detail; retain sanitized evidence outside the repository.

Worker liveness uses the release's process-only `/` route. Its `/api/health`
also checks PostgreSQL and Redis and can restart healthy workers on dependency
outages. The pinned chart has no worker readiness probe. A successful pod count
is not proof of completed ingestion; run the Python outcome.

The example does not promise mail delivery, enterprise-only capabilities,
multi-replica availability, recovery, or performance. Ordinary optional product
integrations still need their own credentials and infrastructure.

## Costs and cleanup

EKS and node charges continue independently of this stack. Account for RDS
hourly and storage charges, 25Gi of application EBS, S3 requests/storage and
existing NAT/data transfer. Reconcile actual spend and the cleanup reserve
before apply; no precise total has been measured for this source.

Data protection is enabled by default. RDS keeps seven days of backups and takes
a final snapshot on an intentional destructive teardown. Bucket/PVC/RDS Pulumi
protection prevents accidental destroy, and a nonempty bucket will not auto-empty.
This is retention protection, not a coherent backup or tested restore.

For a synthetic deploy-and-delete trial, set this before the first preview:

```sh
pulumi config set disposable true
```

That explicit mode removes data protection, enables bucket emptying and requests
no final RDS snapshot or retained automated backups. Finish by stopping the
port-forward and running:

```sh
pulumi destroy
```

For an existing protected deployment, first decide what data to retain and
export it through the product. Switching `disposable` to true is an irreversible
data-destruction decision when followed by destroy; review its preview.
Do not delete the existing EKS foundation. Independently reconcile the stack's
RDS instances, snapshots/backups, S3 objects/multipart uploads, PVC/PV/EBS volumes,
IAM role/policy and namespace after cleanup, preserving encrypted state until
that inventory is complete.

## Attribution and maintenance

References: [Langfuse Helm](https://langfuse.com/self-hosting/deployment/kubernetes-helm),
[headless initialization](https://langfuse.com/self-hosting/administration/headless-initialization),
[ClickHouse support](https://langfuse.com/self-hosting/deployment/infrastructure/clickhouse),
[v4 changes](https://langfuse.com/self-hosting/upgrade/upgrade-guides/upgrade-v3-to-v4),
and [OpenTelemetry ingestion](https://langfuse.com/docs/observability/get-started).

`llm-powerhouse` already includes a Pulumi AWS ECS Langfuse implementation;
this example focuses on private EKS ingestion rather than a GPU inference stack.
Langfuse's MIT core excludes its separately licensed enterprise directories.
Official images include EE material; no enterprise license or feature is enabled
here. Preserve the notices in `licenses/`. Chart is MIT, ClickHouse Apache-2.0,
and Valkey BSD-3-Clause. Their bundled dependencies retain their own licenses.
This example's authored code is licensed under the [MIT License](LICENSE) and
is provided as-is, without warranty or a support commitment. Third-party
components retain their own licenses and notices.

During private review, use this repository's Issues for support and contribution
proposals. Do not include credentials, state or private logs. For sensitive
reports, contact the organization administrators privately.
