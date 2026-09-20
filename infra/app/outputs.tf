output "board_url" {
  description = "The dispatch board."
  value       = "http://${aws_lb.main.dns_name}"
}

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "service_name" {
  value = aws_ecs_service.app.name
}

output "log_group" {
  description = "Where the task writes. `aws logs tail <this> --follow`."
  value       = aws_cloudwatch_log_group.app.name
}

output "workspace_filesystem_id" {
  description = "The event log's EFS volume. Deleting this deletes the business's history."
  value       = aws_efs_file_system.workspace.id
}
