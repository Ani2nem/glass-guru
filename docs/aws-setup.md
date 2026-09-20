# AWS setup

Two things, at two different times. Only the first is needed now.

| When | What | Why |
|---|---|---|
| Now | Bedrock model access + local credentials | Run the agents against a real model |
| Phase 3 | GitHub OIDC deploy role | CI/CD with no stored secrets |

**This repository is public.** No credential, key, account number, or ARN containing
an account id should ever be committed. `.gitignore` covers `.env*` and the local
workspace; nothing in the code reads a secret from a file.

---

## 1. Enable Bedrock model access

Model access is off by default and must be granted per region. Pick **one** region and
use it consistently - `us-east-1` and `us-east-1` both carry the Nova models.

1. AWS console -> **Amazon Bedrock** -> **Model access** (left nav, under Configure).
2. **Modify model access**, tick **Amazon Nova Lite**, submit.
3. Amazon-owned models are usually granted immediately. Confirm the row reads
   **Access granted** before moving on.

Nova is reached through a *cross-region inference profile*, whose id carries a
geography prefix: `us.amazon.nova-lite-v1:0`. That matters for the IAM policy below,
because the profile and the underlying model are separate resources.

---

## 2. Local credentials

### Recommended: IAM Identity Center (no long-lived keys)

Credentials expire on their own, which is the entire point. A leaked short-lived token
is a bad afternoon; a leaked access key is a bad quarter.

```bash
aws configure sso
#   SSO start URL   : https://<your-directory>.awsapps.com/start
#   SSO region      : the region your Identity Center lives in
#   Default region  : us-east-1        (use the one you enabled above)
#   Profile name    : glass-guru

export AWS_PROFILE=glass-guru
aws sso login
```

### Alternative: an IAM user with an access key

Faster if you have no Identity Center, and genuinely worse. If you take this route,
treat the key as perishable: delete it when this project is done, and never paste it
into a file inside the repository.

```bash
aws configure --profile glass-guru     # prompts for key, secret, region
export AWS_PROFILE=glass-guru
```

---

## 3. Least-privilege policy

Attach this to the permission set (Identity Center) or the user (access key). It grants
exactly one verb on exactly one model, and nothing else.

Replace `ACCOUNT_ID` with your account number, and `us-east-1` with **the region you
enabled model access in**. The inference-profile ARN is regional; getting it wrong
surfaces as an AccessDenied only after account verification completes, which makes
it look like a new problem rather than the same one.
**Do not commit the filled-in version** - it contains your account id.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeNovaLiteOnly",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/amazon.nova-lite-v1:0",
        "arn:aws:bedrock:us-east-1:ACCOUNT_ID:inference-profile/us.amazon.nova-lite-v1:0"
      ]
    },
    {
      "Sid": "ReadModelCatalogue",
      "Effect": "Allow",
      "Action": ["bedrock:ListFoundationModels", "bedrock:GetFoundationModel"],
      "Resource": "*"
    }
  ]
}
```

Both resource lines are required. A cross-region inference profile routes to the
foundation model in whichever region has capacity, so permission is needed on the
profile *and* on the model - granting only the profile produces an AccessDenied that
names a region you never configured, which is a confusing half-hour.

---

## 4. Verify

```bash
aws sts get-caller-identity                      # who am I
aws bedrock list-foundation-models --region us-east-1 \
  --query "modelSummaries[?contains(modelId,'nova-lite')].modelId" --output text

export AWS_REGION=us-east-1
glass-guru triage "Dan called, van 3 won't start"
```

The last command should extract a `van_unavailable` event for `van-3`.

Failure modes, in the order you will meet them:

- **"Your account is currently being verified"** - AWS-side account activation, not
  a permissions problem. Usually under two hours. Nothing to change; wait.
- **AccessDenied naming a region you did not configure** - the inference-profile ARN
  in the policy is for the wrong region, or is missing.
- **"The assistant is unavailable"** - credentials are not reaching boto3 at all.
  Check `AWS_PROFILE` is exported.

---

## 5. A spend guardrail

Nova Lite is inexpensive - the whole test corpus costs cents - but a budget alarm is
five minutes and removes a category of worry.

AWS console -> **Billing** -> **Budgets** -> **Create budget** -> Monthly cost, $5,
alert at 80%. That is far above anything this project should reach; if it fires,
something is wrong rather than merely busy.

---

## Optional: LangSmith

Only needed to see agent traces in a UI. Without it, tracing degrades to no-ops and
everything still works.

```bash
export LANGSMITH_API_KEY=...        # free tier at smith.langchain.com
export LANGSMITH_PROJECT=glass-guru
```

Local spans need no account at all:

```bash
make trace CMD=commit
```

---

## Not needed

- **Google Maps.** The traffic layer is designed for it, but OSRM covers routing and
  the synthetic traffic profile covers congestion. No key, no spend.
- **A database.** The event log is file-backed and survives restarts. Postgres arrives
  with deployment.
- **Anything for the tests.** The whole suite runs offline against a frozen travel
  snapshot and a scripted model provider.

---

## Phase 3: GitHub OIDC (later, not now)

When CI/CD lands, GitHub Actions authenticates by exchanging a short-lived OIDC token
for a role - no access keys in repository secrets, nothing to rotate or leak. The trust
policy is scoped to this repository and the `main` branch specifically, so a fork or a
pull request branch cannot assume it. Written up when we get there.
