variable "region" {
  type    = string
  default = "us-east-1"
}

variable "image" {
  description = <<-DESC
    The image to run, by digest.

    A digest rather than a tag, deliberately. `:latest` means the running function and
    the commit that produced it can only be correlated by timestamp, and a rollback
    becomes a guess. The deploy workflow substitutes the digest it just pushed.
  DESC
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.image))
    error_message = "Deploy by digest, not by tag: <registry>/<repo>@sha256:<64 hex>."
  }
}

variable "deploy_role_name" {
  description = "The CI deploy role from the bootstrap stack, granted rights to this stack."
  type        = string
  default     = "glass-guru-deploy"
}

variable "memory_mb" {
  description = <<-DESC
    Memory, which on Lambda also decides CPU.

    1769MB is one vCPU; 2048 is a little over. CP-SAT is the reason this is not the
    128MB minimum, and the solve times in docs/scaling.md were measured against roughly
    one core, so this is the setting that keeps them honest. Lower it and quotes get
    slower; raise it and each request costs proportionally more per second while
    finishing sooner, which is close to a wash until the solver stops being the
    bottleneck.
  DESC
  type        = number
  default     = 2048
}

variable "timeout_seconds" {
  description = <<-DESC
    The ceiling on one request.

    Generous on purpose. A five-day horizon at sixty jobs measured 39 seconds, which is
    already past what API Gateway would allow and comfortably inside this. The load
    balancer this replaced capped out at 120.
  DESC
  type        = number
  default     = 300
}

variable "public" {
  description = <<-DESC
    Whether the function URL itself is reachable without an AWS signature.

    True means AuthType NONE, and the application's own API key is then the only thing
    between the internet and this business's schedule - so `api_key` is required with
    it, and a precondition in main.tf refuses the combination that leaves the door open.

    False means AuthType AWS_IAM: every request must be SigV4-signed, which is right
    the moment real customer addresses are in it, and which a browser cannot do.
  DESC
  type        = bool
  default     = true
}

variable "api_key" {
  description = <<-DESC
    Shared key the application requires on every /api call.

    A shared key rather than JWTs, deliberately: there is one dispatcher and no
    identity provider, and a signing key nobody rotates is worse than a shared secret
    somebody does. This is the seam that becomes a real dependency when there are
    users.

    Pass it with -var or TF_VAR_api_key; never commit it. Health and readiness stay
    open, because a load balancer and a deploy smoke test decide whether the container
    works and neither can hold a secret.
  DESC
  type        = string
  default     = ""
  sensitive   = true

  validation {
    condition     = var.api_key == "" || length(var.api_key) >= 24
    error_message = "An API key shorter than 24 characters is not worth having."
  }
}

variable "travel_mode" {
  description = <<-DESC
    frozen, warm, synthetic or auto.

    `frozen` is the deployed default: real road distances from the committed snapshot,
    no routing backend to run, and a miss raises rather than silently falling back.
    `warm` adds live OSRM for addresses the snapshot has never seen, which is what a
    deployment quoting arbitrary new customers needs - set osrm_url with it.
  DESC
  type        = string
  default     = "frozen"

  validation {
    condition     = contains(["frozen", "warm", "synthetic", "auto"], var.travel_mode)
    error_message = "One of: frozen, warm, synthetic, auto."
  }
}

variable "osrm_url" {
  description = "Only read when travel_mode is warm or osrm."
  type        = string
  default     = ""
}

variable "model_ids" {
  description = <<-DESC
    Which Bedrock models the running application may invoke.

    Named rather than wildcarded: this is the credential an attacker reaches first if
    they get into the function, and "any model in the account" is an expensive thing to
    hand out. The first entry is what the agents use.
  DESC
  type        = list(string)
  default = [
    "us.amazon.nova-lite-v1:0",
    "amazon.nova-lite-v1:0",
  ]
}

variable "log_retention_days" {
  description = "Traces go to LangSmith; CloudWatch holds the operational tail."
  type        = number
  default     = 30
}

variable "alarm_topic_arns" {
  description = <<-DESC
    SNS topics to notify. Empty by default.

    The alarms exist and evaluate either way - the console shows state, and the history
    is there after an incident. Wiring them to a phone is a decision about who is on
    call, which this stack should not make on anyone's behalf.
  DESC
  type        = list(string)
  default     = []
}
