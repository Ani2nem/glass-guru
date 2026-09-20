resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days
}

# The two alarms worth having at this size, and no more. An alarm nobody acts on trains
# people to close the tab.

resource "aws_cloudwatch_metric_alarm" "unhealthy" {
  alarm_name          = "${local.name}-no-healthy-tasks"
  alarm_description   = "Nothing is serving the board. This is the outage alarm."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  statistic           = "Minimum"
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  period              = 60
  evaluation_periods  = 3
  # A deploy briefly has no healthy host. Missing data during one is not an outage.
  treat_missing_data = "notBreaching"

  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
    TargetGroup  = aws_lb_target_group.app.arn_suffix
  }

  alarm_actions = var.alarm_topic_arns
  ok_actions    = var.alarm_topic_arns
}

resource "aws_cloudwatch_metric_alarm" "server_errors" {
  alarm_name          = "${local.name}-5xx-from-the-app"
  alarm_description   = "The app is returning errors. Solver failures surface here first."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  statistic           = "Sum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 5
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
    TargetGroup  = aws_lb_target_group.app.arn_suffix
  }

  alarm_actions = var.alarm_topic_arns
}
