# Everything that has to exist before anything else can be deployed, and that CI must
# never be able to change.
#
# Applied once, by a human, with credentials CI does not have. It creates the state
# bucket the main stack keeps its state in, the trust relationship that lets GitHub
# assume a role without a stored secret, and the registry images are pushed to.
#
# Kept separate from the application stack for one reason: this stack grants the
# permissions that stack runs with. Folding them together would mean the pipeline held
# the power to widen its own access, and a pull request that edited an IAM policy would
# be a pull request that escalated privilege.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
  # Deliberately local. This stack's state describes the bucket every other stack's
  # state lives in, so it cannot live there itself. It is small, it changes about once
  # a year, and it is committed nowhere - see the README in this directory.
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = "glass-guru"
      ManagedBy = "terraform"
      Stack     = "bootstrap"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  # Every claim GitHub can present for this repository. Written out rather than
  # wildcarded so that widening it is a visible diff.
  subject_main = "repo:${var.github_repository}:ref:refs/heads/main"
  subject_pr   = "repo:${var.github_repository}:pull_request"
}

# ---------------------------------------------------------------- terraform state

resource "aws_s3_bucket" "state" {
  bucket = "glass-guru-tfstate-${local.account_id}"

  # State is the only record of what exists. Losing it means reconciling reality by
  # hand; deleting it by accident means doing that under pressure.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ------------------------------------------------------------------------ registry

resource "aws_ecr_repository" "app" {
  name                 = "glass-guru"
  image_tag_mutability = "IMMUTABLE"

  # Images are deployed by digest, but a scan result is worth having anyway.
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 20 images; storage is cheap but not free."
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 20
      }
      action = { type = "expire" }
    }]
  })
}

# ---------------------------------------------------------------- github identity

# The whole point of Phase 3's CI: GitHub proves who it is with a short-lived token it
# gets from AWS, and there is no access key anywhere to leak, rotate, or find in a log.
resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = var.github_oidc_thumbprints
}
