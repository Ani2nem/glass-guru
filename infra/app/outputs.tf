output "board_url" {
  description = "The dispatch board. HTTPS, no certificate to manage, no load balancer."
  value       = aws_lambda_function_url.app.function_url
}

output "function_name" {
  value = aws_lambda_function.app.function_name
}

output "log_group" {
  description = "Where the function writes. `aws logs tail <this> --follow`."
  value       = aws_cloudwatch_log_group.app.name
}

output "workspace_bucket" {
  description = "The event log and every plan version. Deleting this deletes the business's history."
  value       = aws_s3_bucket.workspace.id
}
