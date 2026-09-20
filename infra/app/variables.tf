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
    Whether anyone with the URL can open the board.

    True by default because a demo nobody can open is not a demo, and the board carries
    synthetic data for a fictional business. Set false and the function URL requires a
    signed request, which is the right setting the moment real customer addresses are
    in it.
  DESC
  type        = bool
  default     = true
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
