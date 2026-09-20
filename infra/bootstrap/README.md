# Bootstrap

Applied once, by a human, with credentials CI does not have.

This stack creates the things that must exist before anything can be deployed, and
that the deployment pipeline must never be able to change: the bucket the application
stack keeps its state in, the trust relationship that lets GitHub Actions get AWS
credentials without a stored secret, and the registry images are pushed to.

It is separate from the application stack for one reason.
This stack grants the permissions that stack runs with.
Folding them together would mean the pipeline held the power to widen its own access, and a pull request that edited an IAM policy would be a pull request that escalated privilege.

## What it costs

Effectively nothing: a nearly empty S3 bucket, an OIDC provider, two IAM roles, and an ECR repository that is billed by the gigabyte once images land in it.
Under a dollar a month until the application stack exists.

## Applying it

```bash
export AWS_PROFILE=glass-guru
terraform -chdir=infra/bootstrap init
terraform -chdir=infra/bootstrap apply
```

The state for *this* stack stays local, because its state describes the bucket every other stack's state lives in and so cannot live there itself.
It is small and changes about once a year.
`terraform.tfstate` here is gitignored: it contains account identifiers and this repository is public.

Then follow the `setup_instructions` output, which prints exactly what to paste into the repository's Actions secrets and variables.

## The two roles

Two, not one, and the split is enforced twice over - in the trust policy by which ref may assume which role, and in the permissions policy by what each may then do.

| Role | Assumable from | May |
|---|---|---|
| `glass-guru-ci` | any branch or pull request in this repo | invoke the named eval models, and nothing else |
| `glass-guru-deploy` | `refs/heads/main` only | push to this ECR repository, read and write the state object |

The eval role runs on every pull request, so it gets exactly one capability: calling a model.
Giving it the deploy role's permissions would mean any pull request could deploy, which is the failure OIDC exists to prevent - a short-lived credential scoped to everything is still scoped to everything.

The eval role's model list is written out rather than wildcarded.
It is assumable from any pull request in the repository, so "whatever Bedrock offers" would mean a pull request could run the suite against the most expensive model available and charge it to the account.

The deploy role's subject condition is `StringEquals` on one exact ref, not `StringLike` on a prefix.
A branch named `main-oops` matches `main*`.

## Account identifiers

The account id is a GitHub *secret*, not a variable, because this repository is public and an account id is a free head start for anyone enumerating role names.
Nothing in this directory contains one; they arrive at apply time from the caller's identity.
