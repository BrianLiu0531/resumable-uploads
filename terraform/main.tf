# S3 bucket for the demo, and a least-privilege IAM user for the app (which runs on Render, outside
# AWS, so it cannot use an IAM role). Applied once by hand; state stays local (see README).
#
# Cost guard rails: objects expire after 1 day, unfinished multipart uploads are aborted after 1 day
# (their parts are billed otherwise), no versioning (deleted data is really gone), and the app itself
# caps uploads at 50 MiB per file and 1 GiB per day.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "resumable-uploads", ManagedBy = "terraform" }
  }
}

variable "region" {
  type    = string
  default = "ap-southeast-2"
}

variable "name" {
  type    = string
  default = "resumable-uploads"
}

variable "prefix" {
  description = "Key prefix the app writes under (S3_PREFIX)"
  type        = string
  default     = "uploads/"
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "uploads" {
  bucket        = "${var.name}-demo-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # demo data only; lets `terraform destroy` remove a non-empty bucket
}

resource "aws_s3_bucket_ownership_controls" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  rule {
    object_ownership = "BucketOwnerEnforced" # no ACLs
  }
}

resource "aws_s3_bucket_public_access_block" "uploads" {
  bucket                  = aws_s3_bucket.uploads.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id

  rule {
    id     = "expire-demo-files"
    status = "Enabled"
    filter {}
    expiration {
      days = 1
    }
  }

  rule {
    id     = "abort-unfinished-multipart-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# ---- App credentials ----

resource "aws_iam_user" "app" {
  name = "${var.name}-app"
}

data "aws_iam_policy_document" "app" {
  statement {
    sid = "Objects"
    actions = [
      "s3:PutObject", "s3:GetObject", "s3:DeleteObject",
      "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts",
    ]
    resources = ["${aws_s3_bucket.uploads.arn}/${var.prefix}*"]
  }
  statement {
    sid       = "ListOwnPrefix"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.uploads.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${var.prefix}*"]
    }
  }
}

resource "aws_iam_user_policy" "app" {
  name   = "s3-uploads"
  user   = aws_iam_user.app.name
  policy = data.aws_iam_policy_document.app.json
}

output "bucket" {
  value = aws_s3_bucket.uploads.bucket
}

output "app_user" {
  description = "Create its access key with scripts/create-access-key.sh (kept out of Terraform state)"
  value       = aws_iam_user.app.name
}
