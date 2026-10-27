#!/usr/bin/env bash
# Creates an access key for the app's IAM user and appends it to an env file OUTSIDE the repository,
# so you can paste the values into Render. The secret is never printed and never in Terraform state.
#
# usage: scripts/create-access-key.sh <env file>   e.g. ../deploy.env
set -euo pipefail
OUT="$1"
AWS_CLI="${AWS_CLI:-aws}"
command -v "$AWS_CLI" >/dev/null 2>&1 || AWS_CLI="/c/Program Files/Amazon/AWSCLIV2/aws.exe"
case "$(cd "$(dirname "$OUT")" && pwd)" in "$(git rev-parse --show-toplevel)"*) echo "refusing to write inside the repository" >&2; exit 1;; esac

KEY=$("$AWS_CLI" iam create-access-key --user-name resumable-uploads-app --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text | tr -d '\r')
{
  echo ""
  echo "# resumable-uploads (Render environment variables)"
  echo "RESUMABLE_AWS_ACCESS_KEY_ID=$(echo "$KEY" | cut -f1)"
  echo "RESUMABLE_AWS_SECRET_ACCESS_KEY=$(echo "$KEY" | cut -f2)"
} >> "$OUT"
echo "Access key created and appended to $OUT"
