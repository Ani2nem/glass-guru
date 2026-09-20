variable "region" {
  type    = string
  default = "us-east-1"
}

variable "image" {
  description = <<-DESC
    The image to run, by digest.

    A digest rather than a tag, deliberately. `:latest` means the running task and the
    commit that produced it can only be correlated by timestamp, and a rollback becomes
    a guess. The deploy workflow substitutes the digest it just pushed.
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

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "allowed_cidrs" {
  description = <<-DESC
    Who may reach the board.

    Defaults to the whole internet because a demo nobody can open is not a demo, and
    the board carries synthetic data for a fictional business. Anything with real
    customer addresses in it should be an office range or behind a VPN, and this is
    the one variable to change to get there.
  DESC
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "container_port" {
  type    = number
  default = 8000
}

variable "task_cpu" {
  description = <<-DESC
    CP-SAT is the reason this is not the 256 minimum.

    Measured on this machine: a 25-job day solves in about ten seconds at the batch
    budget and a quote in under two. Fargate vCPU units are not directly comparable, so
    treat 1024 as a starting point and read the actual solve times out of the traces
    before changing it - the solver reports its own duration, so this is measurable
    rather than a matter of taste.
  DESC
  type        = number
  default     = 1024
}

variable "task_memory" {
  description = "OR-Tools holds the whole model in memory; 2GB is comfortable for a 5-day horizon."
  type        = number
  default     = 2048
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
    they get into the container, and "any model in the account" is an expensive thing
    to hand out. The first entry is what the agents use.
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
