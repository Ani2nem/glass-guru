# CI, CD, and what each is allowed to do

## The gate

`ci.yml` runs on every pull request and on pushes to `main`.

It is split on credentials rather than on speed.

| Job | Needs AWS | Runs on |
|---|---|---|
| lint, types, tests | no | everything, forks included |
| dispatch board | no | everything |
| evals, tiers 0 and 3 | no | everything |
| evals, tiers 1, 2 and 4 | yes | this repository only |
| scorecard | no | everything, including red builds |
| gate | no | everything |

The half that needs nothing is the half that catches real defects, and it must never be skippable.
A fork cannot assume the eval role, and handing it credentials so the table looks symmetric would be the worst thing in this repository.
When the model tiers do not run, the scorecard says so rather than showing three fewer green rows.

Both halves compare against `evals/baseline.json`.
Thresholds catch something being broken; the baseline catches something sliding while still technically passing, which is how quality usually degrades - not in one visible step but in a series of small ones nobody objected to.
The tolerance that decides what counts as a slip is one constant, shared between the gate and the comment, because two copies would eventually disagree on a blocked merge.

`gate` is the single required check to protect the branch with, so adding a job later does not mean editing branch protection.
It treats a skipped job as acceptable and any other non-success as blocking - `needs` alone is not enough, because a skipped dependency would otherwise let it pass.

## Deploying

`deploy.yml` is manual only.
Merging to `main` does not deploy; someone triggers it and types `deploy` to confirm.

It can replace the running image and nothing else.
It cannot change the function's configuration, its URL, its permissions, or the bucket holding the event log, because the role it assumes has no such permission - it can push to one ECR repository and call `UpdateFunctionCode` on one function.
Infrastructure changes are a human running `terraform apply` after reading a plan.

A pipeline that can apply arbitrary Terraform is a pipeline that can destroy the business's history, and "every change is reviewed" is a weaker control than "the credential cannot do it".

The image is deployed by digest.
A tag means the running task and the commit that produced it can only be correlated by timestamp, and a rollback becomes a guess.

## Credentials

There are none.

GitHub presents a short-lived OIDC token and AWS exchanges it for temporary credentials.
Nothing is stored, so nothing can leak, expire, or be found in a log.

Two roles, and the split is enforced twice - in the trust policy by which ref may assume which role, and in the permissions policy by what each may then do:

| Role | Assumable from | May |
|---|---|---|
| `glass-guru-ci` | any branch or pull request in this repo | invoke two named Bedrock models |
| `glass-guru-deploy` | `refs/heads/main` only | push to one ECR repository, replace the code of one Lambda function |

### The one that will waste your afternoon

This repository has GitHub's **immutable subject claims** enabled, so the `sub` in the token is not what the documentation examples show:

```
repo:Ani2nem@96967353/glass-guru@1377693884:ref:refs/heads/main     actual
repo:Ani2nem/glass-guru:ref:refs/heads/main                         what a trust policy usually says
```

The owner and repository are identified by numeric id, so a repository that is renamed, transferred, or deleted and recreated does not inherit the trust its name used to carry.
This is strictly better, and a trust policy written against the name form matches nothing.

The only symptom is:

```
Could not assume role with OIDC: Not authorized to perform sts:AssumeRoleWithWebIdentity
```

which says nothing about why, and looks exactly like a dozen other misconfigurations.
Check which form is in use with:

```bash
gh api repos/OWNER/REPO/actions/oidc/customization/sub
```

`use_immutable_subject: true` means the ids are required.
`infra/bootstrap/variables.tf` carries them, and setting `github_owner_id = null` falls back to the name form.

## Running the pieces locally

```bash
make check        # what the quality job runs
make eval-offline # what the credential-free eval job runs
make scorecard    # the comment CI posts, rendered locally
make tf-check     # validate and format-check both terraform stacks
make image        # build the container as it is deployed
make image-run    # run it and print what its health endpoints say
```

## Health endpoints

Two, and the difference matters.

`/api/health` is liveness and deliberately does no work.
A liveness probe that touches the solver would stall whenever a solve is holding the worker threads, turning a slow minute into a restart.
It is also what the Lambda Web Adapter waits for before forwarding the first request, so a cold start cannot serve a half-started app.

`/api/ready` is readiness: the process is up, but can it plan?
It checks the three things the image copies selectively and could stop copying - business parameters, the travel snapshot, and the built board - and returns 503 if any is missing.
This is what the deploy smoke test asserts, and being the first request after a deploy, it also pays the cold start and proves it is survivable.
