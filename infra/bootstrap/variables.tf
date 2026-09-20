variable "region" {
  description = "Where everything lives. Bedrock model availability decided this."
  type        = string
  default     = "us-east-1"
}

variable "github_repository" {
  description = "owner/name. Every trust policy in this stack is scoped to it."
  type        = string
  default     = "Ani2nem/glass-guru"

  validation {
    condition     = can(regex("^[^/]+/[^/]+$", var.github_repository))
    error_message = "Must be owner/name - a bare name would scope the trust policy to nothing."
  }
}

variable "github_oidc_thumbprints" {
  description = <<-DESC
    Certificate thumbprints for GitHub's OIDC issuer.

    AWS stopped checking these for well-known issuers in 2023, and GitHub rotates the
    certificate, so pinning a stale value no longer breaks anything - but the field is
    still required. Kept as a variable so a rotation is a value change rather than
    an edit to the resource.
  DESC
  type        = list(string)
  default     = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

variable "eval_model_ids" {
  description = <<-DESC
    Exactly which models CI may invoke.

    Listed rather than wildcarded: the eval role is assumable from any pull request in
    the repository, so "whatever Bedrock offers" would mean a pull request could run
    the suite against the most expensive model available and charge it to the account.
  DESC
  type        = list(string)
  default = [
    "amazon.nova-lite-v1:0",
    "us.amazon.nova-lite-v1:0",
  ]
}

variable "create_oidc_provider" {
  description = <<-DESC
    Create the GitHub OIDC provider, or adopt the one already in the account.

    There can be only one per account, and any account that has previously connected a
    repository to GitHub Actions already has it. Set false to reuse it. This changes
    nothing about who can do what: the provider only establishes that a token really
    came from GitHub, and the role trust policies decide the rest.
  DESC
  type        = bool
  default     = true
}

variable "github_owner_id" {
  description = <<-DESC
    Numeric account id of the repository owner, or null for the mutable subject form.

    GitHub can issue OIDC tokens with an *immutable* subject claim, which identifies the
    repository by numeric id rather than by name:

      repo:Ani2nem@96967353/glass-guru@1377693884:ref:refs/heads/main

    rather than

      repo:Ani2nem/glass-guru:ref:refs/heads/main

    This is strictly better - a repository that is deleted and recreated, renamed, or
    transferred gets new ids and does not inherit the old one's trust - and it is on for
    this repository. A trust policy written against the name form silently matches
    nothing, and the only symptom is "Not authorized to perform
    sts:AssumeRoleWithWebIdentity", which says nothing about why.

    Check with:
      gh api repos/OWNER/REPO/actions/oidc/customization/sub
    Set both ids to null if `use_immutable_subject` is false there.
  DESC
  type        = number
  default     = 96967353
}

variable "github_repository_id" {
  description = "Numeric id of the repository. See github_owner_id."
  type        = number
  default     = 1377693884
}
