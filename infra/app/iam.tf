# Two task roles, for the same reason the CI roles are two.
#
# The execution role is what ECS itself uses to start the container: pull the image,
# open a log stream. The task role is what the *application* holds while running. They
# are separated so that a compromised application cannot pull other images or write to
# other log groups, and so that the list of things the app may do reads as a list of
# things the app actually does.

data "aws_iam_policy_document" "assume_task" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# ---------------------------------------------------------------- execution role

resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  description        = "Used by ECS to start the task: pull the image, open a log stream."
  assume_role_policy = data.aws_iam_policy_document.assume_task.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ---------------------------------------------------------------------- task role

resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  description        = "Held by the application while it runs."
  assume_role_policy = data.aws_iam_policy_document.assume_task.json
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
    # get into the container, and "any Bedrock model" is an expensive thing to hand out.
    resources = concat(
      [for id in var.model_ids : "arn:aws:bedrock:*::foundation-model/${id}"],
      [for id in var.model_ids : "arn:aws:bedrock:*:${local.account_id}:inference-profile/${id}"],
    )
  }

  statement {
    sid    = "MountTheEventLog"
    effect = "Allow"
    actions = [
      "elasticfilesystem:ClientMount",
      "elasticfilesystem:ClientWrite",
    ]
    resources = [aws_efs_file_system.workspace.arn]
    condition {
      test     = "StringEquals"
      variable = "elasticfilesystem:AccessPointArn"
      values   = [aws_efs_access_point.workspace.arn]
    }
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "runtime-access"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# ------------------------------------------------- what the deploy role may change

# Attached here rather than in the bootstrap stack because these ARNs only exist once
# this stack does. Writing the policy earlier would have meant writing it against
# guesses, which is how a policy ends up with a wildcard in it.
data "aws_iam_policy_document" "deploy_this_stack" {
  statement {
    sid    = "RollTheService"
    effect = "Allow"
    actions = [
      "ecs:UpdateService",
      "ecs:DescribeServices",
      "ecs:DescribeTaskDefinition",
      "ecs:RegisterTaskDefinition",
      "ecs:ListTasks",
      "ecs:DescribeTasks",
    ]
    resources = ["*"] # RegisterTaskDefinition takes no resource; the rest are scoped below.
  }

  statement {
    sid       = "PassOnlyTheseRoles"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.execution.arn, aws_iam_role.task.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "deploy_this_stack" {
  name   = "deploy-${local.name}"
  role   = var.deploy_role_name
  policy = data.aws_iam_policy_document.deploy_this_stack.json
}
