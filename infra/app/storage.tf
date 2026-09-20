# The event log's home.
#
# The log is append-only JSONL with exactly one writer. EFS gives it a filesystem that
# outlives a task, which is the whole requirement: a redeploy must not lose the day's
# bookings. Elastic throughput rather than provisioned, because the access pattern is a
# few kilobytes appended per event and a full read on startup - provisioning for that
# would be paying a monthly fee to make an already-instant read instant.

resource "aws_efs_file_system" "workspace" {
  creation_token  = "${local.name}-workspace"
  encrypted       = true
  throughput_mode = "elastic"

  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }

  tags = { Name = "${local.name}-workspace" }
}

resource "aws_efs_mount_target" "workspace" {
  count = length(aws_subnet.public)

  file_system_id  = aws_efs_file_system.workspace.id
  subnet_id       = aws_subnet.public[count.index].id
  security_groups = [aws_security_group.efs.id]
}

# The task runs as uid 10001 and the image's own directories are owned by it, so the
# access point owns the mount too. Without this the mount arrives root-owned and the
# first append fails with a permission error, several minutes into a deploy.
resource "aws_efs_access_point" "workspace" {
  file_system_id = aws_efs_file_system.workspace.id

  posix_user {
    uid = 10001
    gid = 10001
  }

  root_directory {
    path = "/workspace"
    creation_info {
      owner_uid   = 10001
      owner_gid   = 10001
      permissions = "0755"
    }
  }

  tags = { Name = "${local.name}-workspace" }
}
