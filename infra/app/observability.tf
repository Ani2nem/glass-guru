resource "aws_cloudwatch_log_group" "app" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = var.log_retention_days
}

# Two alarms, and no more. An alarm nobody acts on trains people to close the tab.
#
# Both are about the function failing rather than about latency: a cold start is slow
# and normal, a solve is slow and normal, and an alarm that cannot tell those from a
# problem is noise.

resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "${local.name}-errors"
  alarm_description   = "The app is failing requests. Solver and model failures land here first."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 5
  period              = 300
  evaluation_periods  = 1
  # Nobody using it is the normal state for this business at night; no data is not a
  # fault.
  treat_missing_data = "notBreaching"

  dimensions    = { FunctionName = aws_lambda_function.app.function_name }
  alarm_actions = var.alarm_topic_arns
  ok_actions    = var.alarm_topic_arns
}

resource "aws_cloudwatch_metric_alarm" "throttles" {
  alarm_name          = "${local.name}-throttles"
  alarm_description   = "Requests are being refused before they run. Raise reserved concurrency."
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  statistic           = "Sum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  dimensions    = { FunctionName = aws_lambda_function.app.function_name }
  alarm_actions = var.alarm_topic_arns
}
