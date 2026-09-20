# What has to be told to GitHub. The account id is a secret rather than a variable
# because this repository is public, and an account id in a workflow file is a free
# head start for anyone enumerating roles.

output "setup_instructions" {
  description = "Paste-ready: what to configure in the repository's settings."
  value       = <<-TEXT

    Repository -> Settings -> Secrets and variables -> Actions

      Secrets:
        AWS_ACCOUNT_ID     ${local.account_id}

      Variables:
        AWS_REGION         ${var.region}
        CI_ROLE_NAME       ${aws_iam_role.evals.name}
        DEPLOY_ROLE_NAME   ${aws_iam_role.deploy.name}
        ECR_REPOSITORY     ${aws_ecr_repository.app.name}

    The state bucket for the application stack:
      ${aws_s3_bucket.state.id}
  TEXT
}

output "state_bucket" {
  description = "Backend bucket for the application stack."
  value       = aws_s3_bucket.state.id
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "evals_role_arn" {
  description = "Assumed by pull requests. Bedrock only."
  value       = aws_iam_role.evals.arn
}

output "deploy_role_arn" {
  description = "Assumed by main only. Pushes images and applies the stack."
  value       = aws_iam_role.deploy.arn
}
