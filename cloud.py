"""Dedicated managed PostgreSQL and bucket-scoped application identity."""
import json
from typing import Final

import pulumi
import pulumi_aws as aws

import settings as s
from pins import RDS_ENGINE

bucket: Final = aws.s3.BucketV2("trace-files", bucket_prefix=s.resource_prefix + "-",
    force_destroy=s.disposable, opts=s.data_options)
public_block: Final = aws.s3.BucketPublicAccessBlock("trace-files", bucket=bucket.id,
    block_public_acls=True, block_public_policy=True, ignore_public_acls=True,
    restrict_public_buckets=True, opts=s.ao)
ownership: Final = aws.s3.BucketOwnershipControls("trace-files", bucket=bucket.id,
    rule={"object_ownership": "BucketOwnerEnforced"}, opts=s.ao)
encryption: Final = aws.s3.BucketServerSideEncryptionConfigurationV2("trace-files",
    bucket=bucket.id, rules=[{"apply_server_side_encryption_by_default": {
        "sse_algorithm": "AES256"}}], opts=s.ao)
cors: Final = aws.s3.BucketCorsConfigurationV2("trace-files", bucket=bucket.id,
    cors_rules=[{"allowed_origins": ["http://localhost:3000"],
        "allowed_methods": ["GET", "PUT", "POST", "HEAD"], "allowed_headers": ["*"],
        "expose_headers": ["ETag"], "max_age_seconds": 3600}], opts=s.ao)
tls_policy: Final = aws.s3.BucketPolicy("tls-only", bucket=bucket.id,
    policy=bucket.arn.apply(lambda arn: json.dumps({"Version": "2012-10-17", "Statement": [{
        "Effect": "Deny", "Principal": "*", "Action": "s3:*", "Resource": [arn, arn + "/*"],
        "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]})), opts=s.ao)

role: Final = aws.iam.Role("storage", name=s.resource_prefix + "-storage",
    permissions_boundary=s.config.get("permissionsBoundaryArn"),
    assume_role_policy=json.dumps({
    "Version": "2012-10-17", "Statement": [{"Effect": "Allow",
        "Principal": {"Federated": f"arn:aws:iam::{s.account}:oidc-provider/{s.issuer}"},
        "Action": "sts:AssumeRoleWithWebIdentity", "Condition": {"StringEquals": {
            s.issuer + ":aud": "sts.amazonaws.com",
            s.issuer + ":sub": f"system:serviceaccount:{s.namespace}:langfuse-storage"}}}]}),
    opts=s.ao)
role_policy: Final = aws.iam.RolePolicy("storage", name=s.resource_prefix + "-storage", role=role.id,
    policy=bucket.arn.apply(lambda arn: json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation",
            "s3:ListBucketMultipartUploads"], "Resource": arn},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject",
            "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"], "Resource": arn + "/*"}]})),
    opts=s.ao)

db_subnets: Final = aws.rds.SubnetGroup("database", name=s.resource_prefix,
    subnet_ids=s.subnets, opts=s.ao)
# Inline rules have one owner; no standalone security-group rules overlap them.
db_sg: Final = aws.ec2.SecurityGroup("database", name=s.resource_prefix, vpc_id=s.vpc,
    description="Langfuse PostgreSQL from the selected EKS node security group",
    ingress=[{"protocol": "tcp", "from_port": 5432, "to_port": 5432,
        "security_groups": [s.node_sg]}], egress=[], opts=s.ao)
parameters: Final = aws.rds.ParameterGroup("database", name=s.resource_prefix, family="postgres15",
    parameters=[{"name": "rds.force_ssl", "value": "1", "apply_method": "pending-reboot"},
        {"name": "timezone", "value": "UTC"}], opts=s.ao)
database: Final = aws.rds.Instance("database", identifier=s.resource_prefix,
    engine="postgres", engine_version=RDS_ENGINE,
    instance_class="db.t4g.small", allocated_storage=20, storage_type="gp3",
    storage_encrypted=True, db_name="langfuse", username="langfuse",
    password=s.database_password, db_subnet_group_name=db_subnets.name,
    vpc_security_group_ids=[db_sg.id], parameter_group_name=parameters.name,
    publicly_accessible=False, multi_az=False, auto_minor_version_upgrade=False,
    backup_retention_period=0 if s.disposable else 7,
    deletion_protection=not s.disposable, skip_final_snapshot=s.disposable,
    final_snapshot_identifier=None if s.disposable else s.namespace + "-final",
    delete_automated_backups=s.disposable, copy_tags_to_snapshot=True,
    opts=s.data_options)
