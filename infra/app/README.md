# The application stack

A container on Lambda, and an S3 bucket holding the business's history.
That is the whole thing.

Apply it by hand, read the plan first, and destroy it when you are done looking at it.

## What it costs

| | per month |
|---|---|
| Lambda, 2GB, roughly 2,000 requests a day | ~$2.50 |
| S3 (event log and plan versions), ECR, CloudWatch logs | ~$0.50 |
| Function URL | free |
| **Total** | **~$3** |

Idle cost is zero.
Reproduce it from the published us-east-1 rates: $0.0000166667 per GB-second and $0.20 per million requests.

Absent, and each a deliberate saving: no NAT gateway (-$32), no load balancer (-$16), no RDS (-$15), no ElastiCache (-$11), no OSRM task (-$29).

## Why this shape and not the one it replaced

This stack was ECS Fargate behind an application load balancer, because the project plan said ECS Fargate.
That was the wrong default, and it took writing the monthly figure down to see it.

One business, twenty-five jobs a day, one dispatcher.
An always-on task and a load balancer bill 730 hours a month to serve perhaps two hours of work - about **$47** - and neither scales to zero.
Lambda bills for what runs.

Two things had to be true before that worked, and both were checked rather than assumed.

**The event log had to leave local disk.**
A Lambda has none that survives an invocation, and two invocations must see the same history.
It is on S3 now, which also made committing a plan properly atomic: the filesystem store reads the head, compares, then writes, and a second writer landing between the read and the write silently discards the first customer's booking.
On one machine that window is never lost, so the race is invisible; behind a function URL it is a matter of traffic.
`If-Match` on the head object makes it a real compare-and-swap.

**The board had to stop holding a stream open.**
Lambda bills for every second of an open connection.

| one board open for a working day | |
|---|---|
| server-sent events | **$28.80/month** |
| polling every ten seconds | **$0.58/month** |

Left open overnight the streaming figure passes $86, which is more than the container it replaced.
So the deployed board polls and the API refuses to open a stream, while local development keeps server-sent events, where they are free and nicer.
This is the single change without which "serverless is cheaper" would have been false.

### Other choices worth stating

**No VPC.**
The function reaches Bedrock and S3 over their public endpoints.
A Lambda inside a VPC cannot reach anything without a NAT gateway, and a NAT gateway costs more than all the compute here.

**No API Gateway.**
Its HTTP APIs cap an integration at 30 seconds and the limit cannot be raised; a five-day horizon at sixty jobs measured 39.
REST APIs can exceed 29 seconds only through a quota increase that reduces the account's throttle limit in exchange.
A function URL has a fifteen-minute ceiling, gives HTTPS without a certificate to manage, and costs nothing.

**Frozen travel.**
Real road distances from the committed snapshot, so there is no routing backend to keep alive, and a cache miss raises rather than silently falling back to something worse.
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

The image must be given by digest, not by tag, and a variable validation enforces it.
A tag means the running function and the commit that produced it can only be correlated by timestamp, and a rollback becomes a guess.

To push a first image before any deploy has run:

```bash
aws ecr get-login-password | docker login --username AWS --password-stdin "$(terraform -chdir=infra/bootstrap output -raw ecr_repository_url)"
docker buildx build --platform linux/arm64 --provenance=false \
  -t "$(terraform -chdir=infra/bootstrap output -raw ecr_repository_url):bootstrap" --push .
```

### Two things to measure on the first deploy

Neither has a number yet, and both are honest gaps rather than estimates.

**Cold start.** Importing the app measures about half a second locally, but pulling an 850MB image is the larger part and cannot be measured from here.
If it is unacceptable, SnapStart now covers container images; it pairs awkwardly with the Web Adapter, so try it and measure rather than assuming.

**The real monthly bill.** The figure above is arithmetic, not experience.

## Why CI cannot apply this

The deploy workflow can replace the running image and nothing else.
It cannot change the function's configuration, its URL, its permissions, or the bucket holding the event log, because the role it assumes has no such permission - it can push to one ECR repository and call `UpdateFunctionCode` on one function.

That is deliberate.
A pipeline that can apply arbitrary Terraform is a pipeline that can destroy the business's history, and "every change is reviewed" is a weaker control than "the credential cannot do it".

The application's own role is worth reading for the same reason: it may invoke two named models, read and write one bucket, and write its own logs.
It has no `s3:DeleteObject`, because the log is append-only and plan versions are immutable, so a bug that tries to remove one fails loudly instead of quietly erasing a day's bookings.

It also means the first apply is a human one: this stack creates the role CI later uses.

## Tearing it down

```bash
terraform -chdir=infra/app destroy
```

The workspace bucket carries `prevent_destroy`, so this will refuse until you remove that guard deliberately.
That is the intent: everything else here is disposable, and the bucket is every booking, disruption and plan the business ever recorded.
