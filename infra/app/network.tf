# Two availability zones, public subnets only, and no NAT gateway.
#
# A NAT gateway is $32 a month per zone and exists to give private subnets outbound
# internet. This stack does not need it: the task needs to reach ECR, CloudWatch and
# Bedrock, all of which it can reach directly, and nothing needs to reach the task
# except the load balancer. The security group enforces that, which is the control that
# actually matters - a private subnet with a permissive group is not safer than a public
# subnet with a strict one.
#
# What a public subnet does cost is a public IP on the task. If that is unacceptable -
# a compliance boundary, an auditor who counts subnets rather than rules - set
# private_networking and the stack grows private subnets and a NAT gateway.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = { Name = local.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.name }
}

resource "aws_subnet" "public" {
  count = length(local.azs)

  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true

  tags = { Name = "${local.name}-public-${local.azs[count.index]}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count = length(aws_subnet.public)

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# ------------------------------------------------------------------ security groups

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "The only thing reachable from the internet."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-alb" }
}

resource "aws_vpc_security_group_ingress_rule" "alb_in" {
  for_each = toset(var.allowed_cidrs)

  security_group_id = aws_security_group.alb.id
  description       = "Board traffic"
  cidr_ipv4         = each.value
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_out" {
  security_group_id            = aws_security_group.alb.id
  description                  = "To the task, and nowhere else"
  referenced_security_group_id = aws_security_group.task.id
  from_port                    = var.container_port
  to_port                      = var.container_port
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "task" {
  name        = "${local.name}-task"
  description = "The API task. Reachable only from the load balancer."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-task" }
}

resource "aws_vpc_security_group_ingress_rule" "task_in" {
  security_group_id            = aws_security_group.task.id
  description                  = "From the load balancer only - not from the subnet"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = var.container_port
  to_port                      = var.container_port
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "task_out" {
  security_group_id = aws_security_group.task.id
  description       = "ECR, CloudWatch and Bedrock are all reached over 443"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_security_group" "efs" {
  name        = "${local.name}-efs"
  description = "The event log's filesystem. Reachable only from the task."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-efs" }
}

resource "aws_vpc_security_group_ingress_rule" "efs_in" {
  security_group_id            = aws_security_group.efs.id
  description                  = "NFS from the task"
  referenced_security_group_id = aws_security_group.task.id
  from_port                    = 2049
  to_port                      = 2049
  ip_protocol                  = "tcp"
}
