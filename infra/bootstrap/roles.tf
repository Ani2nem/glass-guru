# Two roles, not one.
#
# The eval job runs on every pull request and needs exactly one thing: permission to
# call a model. The deploy job runs only on main and needs to push images and change
# infrastructure. Giving the first the second's permissions would mean any pull request
# could deploy, which is the failure the OIDC setup exists to prevent - a short-lived
# credential scoped to everything is still scoped to everything.
#
# The split is enforced twice over: in the trust policy, by which branch may assume
# which role, and in the permissions policy, by what each may then do.

# ------------------------------------------------------------------ evals (any PR)

data "aws_iam_policy_document" "evals_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # Any branch or pull request in this repository, and nothing else. A fork's
    # workflow presents its own repository in this claim and is refused here, which is
    # the backstop for the `if:` condition in the workflow.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:*"]
    }
  }
}

resource "aws_iam_role" "evals" {
  name                 = "glass-guru-ci"
  description          = "Runs eval tiers 1, 2 and 4 against Bedrock. Model access only."
  assume_role_policy   = data.aws_iam_policy_document.evals_trust.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "evals" {
  statement {
    sid    = "InvokeTheEvalModel"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    # Named models rather than "*". An eval that can invoke anything is an eval that
    # can be edited into a bill.
    resources = concat(
      [for id in var.eval_model_ids : "arn:aws:bedrock:*::foundation-model/${id}"],
      [for id in var.eval_model_ids : "arn:aws:bedrock:*:${local.account_id}:inference-profile/${id}"],
    )
  }

  statement {
    sid       = "ListModelsForTheAwsCheckTarget"
    effect    = "Allow"
    actions   = ["bedrock:ListFoundationModels"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "evals" {
  name   = "bedrock-eval-access"
  role   = aws_iam_role.evals.id
  policy = data.aws_iam_policy_document.evals.json
}

# -------------------------------------------------------------- deploy (main only)

data "aws_iam_policy_document" "deploy_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # Exactly one ref. Not a prefix, not a wildcard: a branch called `main-oops` would
    # match `main*`, and a tag can be moved.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.subject_main]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name                 = "glass-guru-deploy"
  description          = "Pushes images and applies the application stack. main only."
  assume_role_policy   = data.aws_iam_policy_document.deploy_trust.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "PushImages"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # This action does not take a resource.
  }

  statement {
    sid    = "PushToThisRepositoryOnly"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
    ]
    resources = [aws_ecr_repository.app.arn]
  }

  statement {
    sid       = "ReadAndWriteTheStack"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.state.arn}/*"]
  }

  statement {
    sid       = "FindTheStateObject"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "deploy-access"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}

# The application stack's own permissions are attached in a later commit, alongside the
# resources they refer to. Writing them before the stack exists would mean writing them
# against guesses about ARNs, which is how a policy ends up with a wildcard in it.
