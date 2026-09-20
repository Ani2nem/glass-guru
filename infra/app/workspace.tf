# The business's history: the append-only event log and every plan version ever
# committed. This is the only stateful thing in the stack and the only thing whose loss
# would actually matter.

resource "aws_s3_bucket" "workspace" {
  bucket = "${local.name}-workspace-${local.account_id}"

  # Deleting this deletes every booking, every disruption and every plan the business
  # has ever recorded. `terraform destroy` should not be able to do it by accident.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "workspace" {
  bucket = aws_s3_bucket.workspace.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "workspace" {
  bucket = aws_s3_bucket.workspace.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "workspace" {
  bucket                  = aws_s3_bucket.workspace.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Plan versions accumulate and are never read again after a few days, but the event log
# is read on every request, so nothing is expired. Storage here is measured in
# megabytes; a lifecycle rule would save cents and risk the one thing worth keeping.
