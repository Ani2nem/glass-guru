# The application stack: everything that costs money.
#
# Kept separate from infra/bootstrap because that stack grants the permissions this one
# runs with. See infra/bootstrap/README.md.
#
# Shape, and why it is this shape rather than the one in the original plan:
#
# The event log is an append-only JSONL file with exactly one writer by design. That is
# not a limitation to engineer around at this size - one business, 25 jobs a day - it is
# the correct model, and the honest way to deploy it is an EFS volume and exactly one
# task. Two tasks would fork the log. Postgres is the answer when there are genuinely
# concurrent writers or when the pgvector catalog lands; until then it would be a
# persistence layer rewrite to support concurrency nothing needs.
#
# Travel comes from the committed snapshot, so there is no OSRM task and no routing
# backend to keep alive. Real road distances, computed once and frozen. A deployment
# that needs to quote arbitrary new addresses sets travel_mode to "warm" and points
# osrm_url at a routing service; everything for that is a variable, not an edit.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  backend "s3" {
    key          = "app/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
    # bucket is supplied at init time: it is named for the account, and this repository
    # is public. See the README in this directory.
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = "glass-guru"
      ManagedBy = "terraform"
      Stack     = "app"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  name       = "glass-guru"
  account_id = data.aws_caller_identity.current.account_id

  # One task, on purpose. The append-only log has a single writer; a second task would
  # fork it. Stated here as a constant rather than a variable so that raising it is a
  # code change that has to argue with this comment.
  desired_count = 1
}
