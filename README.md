# Private Langfuse tracing on AWS EKS with Python Pulumi

This example deploys Langfuse, an open source tool for recording and inspecting
LLM application traces, inside a Kubernetes cluster you already run in AWS.
Pulumi (infrastructure as code written in Python) creates the databases,
storage and app. A small included Python script then sends a test trace and
reads it back to prove the whole pipeline works.

## What you'll build

- A private Langfuse install (web app plus background worker) in its own
  Kubernetes namespace.
- The storage Langfuse needs: a PostgreSQL database, an S3 bucket, ClickHouse
  (an analytics database for trace data) and Valkey (an in-memory queue and
  cache).
- A ready-to-use admin account, organization, project and API keys.
- A test script, `trace_demo.py`, that sends a two-step trace with a score and
  confirms Langfuse stored exactly that.

A few terms you'll see throughout:

- **EKS** (Amazon Elastic Kubernetes Service) is AWS's managed Kubernetes. This
  example runs inside an EKS cluster you already have. It doesn't create one.
- **RDS** (Amazon Relational Database Service) is AWS's managed database. It
  hosts Langfuse's PostgreSQL database here.
- **IRSA** (IAM Roles for Service Accounts) lets a Kubernetes pod use an AWS
  role through short-lived credentials, so no AWS keys are stored in the
  cluster.
- **OIDC** (OpenID Connect) is an identity standard. EKS uses an OIDC provider
  to vouch for pods when they request IRSA credentials.
- **Helm** is the Kubernetes package manager. A Helm "chart" is a packaged app.
  This example installs the official Langfuse chart.
- **PVC** (PersistentVolumeClaim) is a request for a Kubernetes disk. Here
  those disks are AWS EBS volumes.
- A **trace** is one recorded run of an LLM app, and a **span** is one step
  inside it.

## What gets created

- One private namespace with the official Langfuse Helm release.
- One encrypted private RDS PostgreSQL 15.18 database, 20Gi gp3, db.t4g.small.
- One private, encrypted S3 bucket for events, media and exports. Langfuse
  reaches it through an IAM role limited to that bucket, using IRSA.
- One 20Gi ClickHouse PVC and one 5Gi Valkey PVC, each with a private service.
- A real administrator, organization, project and API keys, created by
  Langfuse's supported headless (no-click) startup setup.

The chart connects to these as external stores, so you don't need a ClickHouse
operator, Keeper, cert-manager, Tailscale, an ingress controller or CI. The
single-container ClickHouse is upstream's community development option. Web
and worker run as separate Deployments, so the chart can scale them
independently. This example uses single-node storage and does not include an
HA deployment or backup-and-restore setup.

The stack doesn't create an EKS cluster, runner, VPC, node group, OIDC provider
or CSI driver. Every app service is ClusterIP (reachable only inside the
cluster). You reach Langfuse through an authenticated `kubectl port-forward` to
your own machine, then sign in or use API keys.

## Before you start

You'll need these already in place:

1. **An AWS account and EKS cluster** in `us-east-2` with Linux AMD64 nodes, a
   managed node group you can target, standard VPC CNI networking, an IAM OIDC
   provider for the cluster, and EBS CSI storage with a `Delete` reclaim policy.
2. **Network inputs:** two private subnets for RDS in different availability
   zones, the cluster's VPC, and the security group your selected nodes use. If
   your pods use security groups for pods, change the RDS rule to match on
   purpose.
3. **A machine to run the commands** that can reach the cluster's private
   Kubernetes API, AWS APIs, container registries and DNS. Its short-lived AWS
   identity needs permission to create the AWS resources and namespace in this
   program.
4. **Tools on that machine:** Python 3.12, uv, AWS CLI, kubectl and Pulumi
   3.273.0, plus the Pulumi AWS provider 7.52.1 and Kubernetes provider 4.34.2.
   This repository doesn't install tools for you. Prefer copies you've already
   installed and scanned.

**Safety first:** malware-scan the exact Python wheels, provider binaries,
chart archive, container image manifests and all image layers before running
them. Do installs, previews and the test script in an isolated sandbox, never
on your everyday workstation. A vulnerability audit is not a malware scan.

## Step 1: Get the code

Clone the repository:

```sh
git clone https://github.com/pulumi-labs/pulumi-example-stack-aws-langfuse-eks.git
cd pulumi-example-stack-aws-langfuse-eks
```

Run every command below in your isolated environment.

## Step 2: Download and verify the chart

Download the Helm chart as a plain file and check its SHA-256 hash. Don't
install anything yet:

```sh
mkdir -p .artifacts
curl --fail --location \
  https://github.com/langfuse/langfuse-k8s/releases/download/langfuse-2.2.4/langfuse-2.2.4.tgz \
  --output .artifacts/langfuse-2.2.4.tgz
printf '%s  %s\n' \
  29e657662e210c0914bfce914d33b61101b890f2d2cda0e871ca8fcaac6fb3f6 \
  .artifacts/langfuse-2.2.4.tgz | sha256sum -c -
```

**Success:** `sha256sum` prints `OK`.

## Step 3: Install Python dependencies

After you've malware-scanned the chart archive, the full wheelhouse (a folder
of downloaded Python wheels) and the provider plugins, create the virtual
environment using the Python you already have installed:

```sh
: "${REVIEWED_WHEELHOUSE:?Set the complete scanned wheelhouse directory}"
uv venv --python 3.12 --no-python-downloads
uv pip sync --python .venv/bin/python --no-index \
  --find-links "$REVIEWED_WHEELHOUSE" --require-hashes \
  --only-binary :all: requirements.lock
```

The lock pins Pulumi, the AWS and Kubernetes providers, and a small Pydantic
response parser. Hashes cover CPython 3.12 Linux ARM64 and AMD64 wheels where
native code is involved. The cluster nodes run AMD64. The exact container image
digests live in `pins.py`. Don't swap them for floating tags, and scan anything
you regenerate before you use it.

## Step 4: Configure the stack

Use a Pulumi secrets backend you control, and keep its encryption key outside
this repository. Set the shell variables from your real platform details, then
create a stack (one deployment of this project) and configure it:

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

The last command prompts you for the first admin's email address.

### Optional settings

- `resourcePrefix` changes the AWS name prefix and namespace, which default to
  `langfuse-<stack>`. Use a unique lowercase DNS label, such as
  `ae4-langfuse`. The database, subnet, parameter and security groups, IAM role
  and bucket prefix all use it.
- `permissionsBoundaryArn` attaches an IAM permissions boundary to the
  application role only.

Neither is required. Resources are tagged with `pulumi-project`,
`pulumi-stack` and `resource-prefix`.

## Step 5: Generate secrets

This generates fresh secrets and stores them encrypted, without printing them:

```sh
for key in databasePassword queuePassword clickhousePassword adminPassword salt sessionSecret encryptionKey
do
  openssl rand -hex 32 | pulumi config set --secret "$key"
done
pulumi config set projectPublicKey "pk-lf-$(openssl rand -hex 32)"
printf 'sk-lf-%s' "$(openssl rand -hex 32)" | pulumi config set --secret projectSecretKey
```

The rules, if you supply your own values instead:

- Ordinary secrets: 32..128 URL-safe characters.
- `encryptionKey`: exactly 64 lowercase hexadecimal characters.
- Project keys: `pk-lf-` or `sk-lf-` followed by 32..128 URL-safe characters.

Never turn on shell tracing (`set -x`) while handling secrets. Save the admin
password somewhere safe, outside source control, before you use it.

Langfuse creates the project and admin account at startup, with or without the
test script. That setup only runs once: changing these values later won't
rotate existing API keys or the admin password. Rotate those in Langfuse's
settings. If you change a Kubernetes Secret the app reads, restart the pods
that use it yourself.

## Step 6: Preview and deploy

Save a preview plan, read it, then apply exactly that plan. After it finishes,
open a port-forward to the web app:

```sh
pulumi preview --save-plan deployment.plan
pulumi up --plan deployment.plan
kubectl -n "$(pulumi stack output namespace)" \
  port-forward service/langfuse-web 3000:3000 --address 127.0.0.1
```

Leave that port-forward running in its own terminal.

## Step 7: Sign in

Open `http://localhost:3000` and sign in with the admin email and password you
configured. Sign-up is turned off.

Under the hood: browser media uploads use S3 presigned URLs, with CORS limited
to this local address. PostgreSQL uses the bundled regional CA with
`verify-full`, and S3 uses HTTPS. ClickHouse and Valkey traffic stays inside the
cluster, uses passwords, and isn't encrypted.

## Step 8: Run the trace test

In a second terminal, in the same directory:

```sh
export LANGFUSE_PUBLIC_KEY="$(pulumi config get projectPublicKey)"
export LANGFUSE_SECRET_KEY="$(pulumi config get projectSecretKey)"
export TRACE_RUN_ID="$(openssl rand -hex 16)"
.venv/bin/python trace_demo.py
unset LANGFUSE_SECRET_KEY LANGFUSE_PUBLIC_KEY TRACE_RUN_ID
```

The script always talks to `127.0.0.1:3000`. It ignores proxy environment
variables, doesn't follow redirects and makes no LLM calls. Each fresh
`TRACE_RUN_ID` produces predictable trace, span and score IDs. The script first
checks that the trace ID isn't already used, then sends the trace and waits up
to 180 seconds for the exact stored result.

**Success** looks like JSON with `status=passed`, the public IDs, score `1` and
six denied requests. Behind that, the script checks that:

- The two spans are `answer` and its child `retrieve`, with the expected input,
  output, environment and trace name.
- The v3 score points at the same trace.
- Missing and wrong project credentials get HTTP 401 for observation reads,
  score reads and OTLP (OpenTelemetry) writes.

This proves more than "the server said OK". The worker has to pull the job off
the queue and write the result before the script can read it back. A partial
OTLP acceptance counts as a failure. The script uses v4 observations and v3
scores because the older trace and score readers aren't available in
events-only mode.

## Troubleshooting

- **Pods stuck in Pending:** the node group probably lacks capacity, the AMD64
  node selector doesn't match, or the storage class can't create disks.
  Requests total about 2.1 CPU and 4.125Gi RAM before platform overhead and
  rollout surge. Limits total 6.5 CPU and 12.5Gi. One m6i.xlarge node is a
  small trial pool, not a capacity guarantee.
- **Database connection failures:** check the node or pod security group path,
  subnet routing, startup logs and the unchanged RDS CA. Never turn off
  certificate checks to get past a startup error. Migration logs can contain
  operational detail, so keep cleaned-up copies outside the repository.
- **Pods running but no data:** worker liveness uses the release's
  process-only `/` route. Its `/api/health` also checks PostgreSQL and Redis
  and could restart healthy workers during a dependency outage. The pinned
  chart has no worker readiness probe. Healthy pods don't prove ingestion
  works, so run the trace test.

## Limits

There's no promise of email delivery, enterprise-only features, multiple
replicas, recovery or performance. Optional product integrations need their own
credentials and infrastructure.

## Costs

Your EKS cluster and nodes cost money whether or not this stack exists. On top
of that, budget for RDS hourly and storage charges, 25Gi of EBS for the app, S3
requests and storage, and existing NAT and data transfer. Check your actual
spend and leave room for cleanup before you deploy. No precise total has been
measured.

## Cleanup

Data protection is on by default. RDS keeps seven days of backups and takes a
final snapshot if you deliberately destroy it. Pulumi protection on the bucket,
PVCs and RDS blocks accidental deletion, and a nonempty bucket won't empty
itself. This protects your data from deletion; it is not a consistent backup.

**For a throwaway trial,** set this before your first preview:

```sh
pulumi config set disposable true
```

That turns off data protection, lets Pulumi empty the bucket, and skips the
final RDS snapshot and retained automated backups. When you're done, stop the
port-forward and run:

```sh
pulumi destroy
```

**For a deployment you care about,** decide what data to keep and export it
through Langfuse first. Switching `disposable` to true and then destroying
deletes the data for good, so read that preview carefully. Never delete the
existing EKS cluster or its foundation.

After cleanup, check by hand that the stack's RDS instances, snapshots and
backups, S3 objects and multipart uploads, PVCs, PVs and EBS volumes, IAM role
and policy, and namespace are gone. Keep your encrypted Pulumi state until
that check is complete.

## How this differs from other examples

`llm-powerhouse` already includes a Pulumi AWS ECS Langfuse setup. This example
focuses on private EKS ingestion instead of a GPU inference stack.

## License and attribution

This example's authored code is licensed under the [MIT License](LICENSE) and
is provided as-is, without warranty or a support commitment. Third-party
components keep their own licenses and notices, preserved in `licenses/`:

- Langfuse's core is MIT, but its enterprise directories are licensed
  separately. The official images include enterprise material. No enterprise
  license or feature is enabled here.
- The Langfuse chart is MIT, ClickHouse is Apache-2.0 and Valkey is
  BSD-3-Clause. Their bundled dependencies keep their own licenses.

## Support and contributing

Use this repository's Issues for questions, bug reports and contribution ideas.
Never include credentials, state or private logs. For sensitive security
reports, contact the organization administrators privately.

## References

- [Langfuse Helm](https://langfuse.com/self-hosting/deployment/kubernetes-helm)
- [Headless initialization](https://langfuse.com/self-hosting/administration/headless-initialization)
- [ClickHouse support](https://langfuse.com/self-hosting/deployment/infrastructure/clickhouse)
- [v4 changes](https://langfuse.com/self-hosting/upgrade/upgrade-guides/upgrade-v3-to-v4)
- [OpenTelemetry ingestion](https://langfuse.com/docs/observability/get-started)
