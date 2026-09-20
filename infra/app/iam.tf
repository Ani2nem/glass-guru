# What the running application may do, and what the deploy pipeline may do to it.
#
# There is no execution role here and no task role, because there is no ECS. Lambda
# uses one role for both purposes, so the list below is exactly the set of things the
# application actually does - and it is short enough to read, which is the point.

data "aws_iam_policy_document" "assume_lambda" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task" {
  name               = "${local.name}-runtime"
  description        = "Held by the application while it runs."
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid    = "InvokeTheAgentModel"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    # Named, not wildcarded. This is the credential an attacker reaches first if they
    # get into the function, and "any Bedrock model" is an expensive thing to hand out.
    resources = concat(
      [for id in var.model_ids : "arn:aws:bedrock:*::foundation-model/${id}"],
      [for id in var.model_ids : "arn:aws:bedrock:*:${local.account_id}:inference-profile/${id}"],
    )
  }

  statement {
    sid    = "ReadAndWriteTheBusinessHistory"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.workspace.arn,
      "${aws_s3_bucket.workspace.arn}/*",
    ]
  }

  # Deliberately absent: s3:DeleteObject. The log is append-only and plan versions are
  # immutable, so nothing the application does needs to remove an object. A bug that
  # tries will fail loudly rather than quietly erasing a day's bookings.

  statement {
    sid       = "WriteItsOwnLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.app.arn}:*"]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "runtime-access"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# ------------------------------------------------- what the deploy role may change

# Attached here rather than in the bootstrap stack because these ARNs only exist once
# this stack does. Writing the policy earlier would mean writing it against guesses,
# which is how a policy ends up with a wildcard in it.
data "aws_iam_policy_document" "deploy_this_stack" {
  statement {
    sid    = "ReplaceTheRunningImage"
    effect = "Allow"
    actions = [
      "lambda:UpdateFunctionCode",
      "lambda:GetFunction",
      "lambda:GetFunctionConfiguration",
    ]
    resources = [aws_lambda_function.app.arn]
  }

  # Deliberately absent: UpdateFunctionConfiguration, AddPermission, CreateFunctionUrl.
  # The pipeline may change what code runs and nothing about how it is reached or what
  # it is allowed to do. Those are terraform's, and a human reads the plan.
}

resource "aws_iam_role_policy" "deploy_this_stack" {
  name   = "deploy-${local.name}"
  role   = var.deploy_role_name
  policy = data.aws_iam_policy_document.deploy_this_stack.json
}
