resource "aws_lb" "main" {
  name               = local.name
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  # A solve is CPU-bound and can run for tens of seconds under the batch budget. The
  # 60s default would cut a legitimate re-optimise off at the knees and report it to
  # the dispatcher as a 504.
  idle_timeout = 120

  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "app" {
  name        = local.name
  port        = var.container_port
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  # Readiness, not liveness. The question the load balancer is asking is "should I send
  # this task traffic", and a task that is up but cannot reach its travel data should be
  # answered no. /api/health would say yes.
  health_check {
    path                = "/api/ready"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # One task and a single writer, so a rolling deploy briefly has two. Drain quickly:
  # there is nothing long-lived to protect except the event stream, which reconnects.
  deregistration_delay = 15
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}
