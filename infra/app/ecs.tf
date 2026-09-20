resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enhanced"
  }
}

resource "aws_ecs_task_definition" "app" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  # ARM. Fargate charges roughly 20% less for it, the image is built for it, and
  # OR-Tools ships aarch64 wheels, so there is nothing to trade away.
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  volume {
    name = "workspace"

    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.workspace.id
      transit_encryption = "ENABLED"

      authorization_config {
        access_point_id = aws_efs_access_point.workspace.id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([
    {
      name  = "api"
      image = var.image
      # A container that cannot start should stop the deploy, not leave a half-rolled
      # service serving a mix of two versions.
      essential = true

      portMappings = [{
        containerPort = var.container_port
        protocol      = "tcp"
      }]

      mountPoints = [{
        sourceVolume  = "workspace"
        containerPath = "/workspace"
        readOnly      = false
      }]

      environment = [
        { name = "GLASS_GURU_WORKSPACE", value = "/workspace/.glass-guru" },
        { name = "GLASS_GURU_TRAVEL", value = var.travel_mode },
        { name = "GLASS_GURU_API_HOST", value = "0.0.0.0" },
        { name = "GLASS_GURU_API_PORT", value = tostring(var.container_port) },
        { name = "GLASS_GURU_OSRM_URL", value = var.osrm_url },
        { name = "GLASS_GURU_MODEL_ID", value = var.model_ids[0] },
        { name = "AWS_REGION", value = var.region },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.app.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "api"
        }
      }

      # Same probe the image declares, restated because ECS does not read the image's
      # HEALTHCHECK. Liveness only - the load balancer asks the readiness question, and
      # a container that restarts itself because travel data is briefly unreachable
      # turns a degraded minute into a crash loop.
      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${var.container_port}/api/health', timeout=4).status == 200 else 1)\""]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }
    }
  ])
}

resource "aws_ecs_service" "app" {
  name            = local.name
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = local.desired_count
  launch_type     = "FARGATE"

  # One writer. A rolling deploy is allowed to briefly run two tasks - the old one is
  # draining and the log tolerates the overlap - but steady state is exactly one.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 200

  # Roll back rather than sit there broken. Without this a bad image leaves the service
  # retrying forever and the only signal is a target group with nothing healthy in it.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = true # No NAT gateway. See network.tf.
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "api"
    container_port   = var.container_port
  }

  # The deploy workflow updates the image, so terraform should not fight it back to
  # whatever was in the variable at last apply.
  lifecycle {
    ignore_changes = [task_definition]
  }

  depends_on = [aws_lb_listener.http]
}
