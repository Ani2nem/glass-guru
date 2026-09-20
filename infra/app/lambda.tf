resource "aws_lambda_function" "app" {
  function_name = local.name
  description   = "Dispatch board, API and solver. One image, started on demand."
  role          = aws_iam_role.task.arn
  package_type  = "Image"
  image_uri     = var.image

  # ARM. Lambda charges about 20% less for it, the image is built for it, and OR-Tools
  # ships aarch64 wheels, so there is nothing to trade away.
  architectures = ["arm64"]

  # CP-SAT is the reason this is not the 128MB minimum, and memory is also how Lambda
  # allocates CPU - 2GB is roughly one vCPU, which is what the solve times in
  # docs/scaling.md were measured against.
  memory_size = var.memory_mb

  # Far more headroom than the load balancer had at 120 seconds, and more than any
  # measured solve needs: a five-day horizon at sixty jobs took 39 seconds. The point
  # is that a slow re-optimise returns an answer rather than a gateway error.
  timeout = var.timeout_seconds

  environment {
    variables = {
      # The whole reason the storage layer moved: a Lambda has no disk that outlives an
      # invocation, and two invocations must see the same history.
      GLASS_GURU_WORKSPACE = "s3://${aws_s3_bucket.workspace.id}/business"
      GLASS_GURU_TRAVEL    = var.travel_mode
      GLASS_GURU_OSRM_URL  = var.osrm_url
      GLASS_GURU_MODEL_ID  = var.model_ids[0]

      # Polling, not streaming. An open stream is billed for its whole duration here,
      # which is the single change that makes this cheaper than a container rather than
      # more expensive. See stream_mode() in api/main.py.
      GLASS_GURU_STREAM = "poll"
    }
  }

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.app.name
  }

  depends_on = [aws_cloudwatch_log_group.app]
}

resource "aws_lambda_function_url" "app" {
  function_name      = aws_lambda_function.app.function_name
  authorization_type = var.public ? "NONE" : "AWS_IAM"

  # Lets a long solve send headers immediately and the body as it comes, instead of
  # buffering the whole response. The Web Adapter in the image is configured to match;
  # they have to agree or nothing streams.
  invoke_mode = "RESPONSE_STREAM"

  cors {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST"]
    allow_headers = ["content-type"]
  }
}
