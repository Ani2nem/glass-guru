# The application stack: a container on Lambda, and an S3 bucket holding the business's
# history. That is the whole thing.
#
# It began as ECS Fargate behind an application load balancer, because the project plan
# said ECS Fargate. That was the wrong default for this workload and it took writing
# down the monthly figure to see it: one business, twenty-five jobs a day, one
# dispatcher. An always-on task and a load balancer bill 730 hours a month to serve
# perhaps two hours of work - about $47 - and neither scales to zero.
#
# Lambda bills for what runs. Idle costs nothing, a function URL gives HTTPS and a
# fifteen-minute request ceiling for free, and the whole stack lands near $3. Two things
# had to be true first, and both were checked rather than assumed:
#
#   the event log had to leave local disk, because a Lambda has none that survives an
#   invocation - it is on S3 now, which also made committing a plan properly atomic
#
#   the board had to stop holding a server-sent-events stream open, because Lambda
#   bills for every second of it: about $29 a month for one board left open during a
#   working day, and more than the container it replaced if left open overnight
#
# There is no VPC. The function reaches Bedrock and S3 over their public endpoints,
# which is what keeps this at $3 rather than $35 - a Lambda in a VPC needs a NAT
# gateway to reach anything, and a NAT gateway costs more than all the compute here.

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

locals {
  name       = "glass-guru"
  account_id = data.aws_caller_identity.current.account_id
}
