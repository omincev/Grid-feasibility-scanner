#!/bin/sh
# One-shot compose service `minio-init` (infra/docker-compose.prod.yml), run on every `up`, idempotent: the
# bucket named in RAW_STORE_URL and the scanner's access key (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY) on the
# local MinIO, with a policy limited to that bucket and without delete, so the raw store stays write-once for
# the scanner. Does nothing when the raw store is elsewhere (Cloudflare R2 later: infra/README.md).
set -eu

case "${FSSPEC_S3_ENDPOINT_URL:-}" in
http://minio:9000 | http://minio:9000/) ;;
*)
	echo "raw store endpoint is not the local MinIO (${FSSPEC_S3_ENDPOINT_URL:-unset}); nothing to do"
	exit 0
	;;
esac
case "$RAW_STORE_URL" in
s3://?*) ;;
*)
	echo "RAW_STORE_URL is not an s3:// URL; nothing to do"
	exit 0
	;;
esac
: "${AWS_ACCESS_KEY_ID:?set in the env file}" "${AWS_SECRET_ACCESS_KEY:?set in the env file}"

bucket=${RAW_STORE_URL#s3://}
bucket=${bucket%%/*}
# Root credentials through the environment, not a config file; the generated passwords are hex (URL-safe).
export MC_HOST_local="http://${MINIO_ROOT_USER}:${MINIO_ROOT_PASSWORD}@minio:9000"

mc mb --ignore-existing "local/$bucket"

policy=/tmp/scanner-raw-store.json
cat >"$policy" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads"],
      "Resource": ["arn:aws:s3:::$bucket"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
      "Resource": ["arn:aws:s3:::$bucket/*"]
    }
  ]
}
EOF
name=scanner-raw-store
mc admin policy create local "$name" "$policy"
mc admin user add local "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY"
if ! mc admin user info local "$AWS_ACCESS_KEY_ID" --json | grep -Eq "\"policyName\": *\"([^\"]*,)?${name}[,\"]"; then
	mc admin policy attach local "$name" --user "$AWS_ACCESS_KEY_ID"
fi
echo "raw store ready: bucket $bucket, access key $AWS_ACCESS_KEY_ID (policy $name)"
