# The application stack

Everything that costs money.
Apply it by hand, read the plan first, and destroy it when you are done looking at it.

## What it costs

| | per month |
|---|---|
| Application load balancer | ~$16 |
| Fargate, 1 task, 1 vCPU / 2GB, ARM64 | ~$18 |
| EFS for the event log | ~$1 |
| CloudWatch logs and ECR storage | ~$2 |
| **Total** | **~$37** |

No NAT gateway, which would be $32 a month on its own and is the single largest line item in a naive VPC.
No RDS and no ElastiCache, because nothing in the application uses them yet.
No OSRM task, because travel comes from the committed snapshot.

The load balancer is the largest cost and it exists to give the board a stable URL.

## Why this shape and not the one in the plan

**One task, and an EFS volume.**
The event log is append-only JSONL with exactly one writer by design.
Two tasks would fork it.
At one business and 25 jobs a day that is not a limitation to engineer around, it is the correct model, and the honest way to deploy it is a filesystem that outlives a task and a `desired_count` of 1.
Postgres becomes the right answer when there are genuinely concurrent writers, or when the pgvector catalog lands - until then it would be a persistence-layer rewrite to support concurrency nothing needs.

**Public subnets, strict security groups, no NAT gateway.**
A NAT gateway exists to give private subnets outbound internet.
The task needs to reach ECR, CloudWatch and Bedrock, all of which it can reach directly, and nothing needs to reach the task except the load balancer.
The security group enforces that by referencing the load balancer's group rather than a CIDR, which is the control that actually matters: a private subnet with a permissive group is not safer than a public subnet with a strict one.
What a public subnet does cost is a public IP on the task; if that is unacceptable, the change is to add private subnets and a NAT gateway, and the security groups do not move.

**Frozen travel.**
Real road distances from the committed snapshot, no routing backend to keep alive, and a cache miss raises rather than silently falling back to something worse.
A deployment that quotes arbitrary new addresses sets `travel_mode = "warm"` and points `osrm_url` at a routing service.
Both are variables, not edits.

## Applying it

```bash
export AWS_PROFILE=glass-guru

terraform -chdir=infra/app init \
  -backend-config="bucket=$(terraform -chdir=infra/bootstrap output -raw state_bucket)"

terraform -chdir=infra/app plan \
  -var "image=$(terraform -chdir=infra/bootstrap output -raw ecr_repository_url)@sha256:<digest>"
```

Read the plan, then `apply` the same arguments.
`terraform -chdir=infra/app output board_url` prints where the board is.

The image must be given by digest, not by tag.
A variable validation enforces it: `:latest` means the running task and the commit that produced it can only be correlated by timestamp, and a rollback becomes a guess.

To push a first image before any deploy has run:

```bash
aws ecr get-login-password | docker login --username AWS --password-stdin "$(terraform -chdir=infra/bootstrap output -raw ecr_repository_url)"
docker buildx build --platform linux/arm64 --provenance=false \
  -t "$(terraform -chdir=infra/bootstrap output -raw ecr_repository_url):bootstrap" --push .
```

## Why CI cannot apply this

The deploy workflow can replace the running image and nothing else.
It cannot create a VPC, edit an IAM policy, or delete the filesystem holding the event log, because the role it assumes has no such permissions - it can push to one ECR repository and roll one ECS service.

That is deliberate.
A pipeline that can apply arbitrary Terraform is a pipeline that can destroy the business's history, and "every change is reviewed" is a weaker control than "the credential cannot do it".

It also means the first apply is a human one: this stack creates the IAM roles that CI later uses.

## Tearing it down

```bash
terraform -chdir=infra/app destroy
```

This deletes the EFS volume, and with it every event the deployed system recorded.
Nothing else in the account is touched - the bootstrap stack, the registry and the roles survive, so bringing it back is one `apply`.
