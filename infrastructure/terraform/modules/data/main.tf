terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

variable "name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "client_security_group_id" {
  type        = string
  description = "Security group of the EKS nodes/pods allowed to reach the data services."
}

variable "kms_key_arn" {
  type = string
}

variable "deletion_protection" {
  type        = bool
  description = "Protect the database from deletion. MUST be true in production."
  default     = true
}

variable "db_instance_class" {
  type    = string
  default = "db.r6g.large"
}

variable "db_allocated_storage_gb" {
  type    = number
  default = 100
}

variable "db_max_allocated_storage_gb" {
  type    = number
  default = 1000
}

variable "db_multi_az" {
  type    = bool
  default = true
}

variable "db_backup_retention_days" {
  type    = number
  default = 14
  validation {
    condition     = var.db_backup_retention_days >= 7
    error_message = "Keep at least 7 days of automated backups."
  }
}

variable "redis_node_type" {
  type    = string
  default = "cache.r6g.large"
}

variable "redis_replicas_per_group" {
  type    = number
  default = 1
}

variable "kafka_instance_type" {
  type    = string
  default = "kafka.m5.large"
}

variable "kafka_version" {
  type    = string
  default = "3.6.0"
}

variable "kafka_ebs_gb" {
  type    = number
  default = 500
}

variable "tags" {
  type    = map(string)
  default = {}
}

# ---------------------------------------------------------------- security groups
# Ingress is only ever "from the client security group". Nothing here opens a port to a CIDR.
resource "aws_security_group" "rds" {
  name_prefix = "${var.name}-rds-"
  vpc_id      = var.vpc_id
  description = "PostgreSQL, reachable only from the application"
  tags        = merge(var.tags, { Name = "${var.name}-rds" })
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "rds" {
  security_group_id            = aws_security_group.rds.id
  referenced_security_group_id = var.client_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_security_group" "redis" {
  name_prefix = "${var.name}-redis-"
  vpc_id      = var.vpc_id
  description = "Redis, reachable only from the application"
  tags        = merge(var.tags, { Name = "${var.name}-redis" })
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "redis" {
  security_group_id            = aws_security_group.redis.id
  referenced_security_group_id = var.client_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}

resource "aws_security_group" "kafka" {
  name_prefix = "${var.name}-kafka-"
  vpc_id      = var.vpc_id
  description = "MSK brokers (SASL/SCRAM over TLS), reachable only from the application"
  tags        = merge(var.tags, { Name = "${var.name}-kafka" })
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "kafka" {
  security_group_id            = aws_security_group.kafka.id
  referenced_security_group_id = var.client_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 9096
  to_port                      = 9096
}

# ---------------------------------------------------------------- PostgreSQL (pgvector)
resource "aws_db_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.private_subnet_ids
  tags       = var.tags
}

resource "aws_db_parameter_group" "this" {
  name_prefix = "${var.name}-pg16-"
  family      = "postgres16"
  description = "Force TLS, log slow statements"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }
  parameter {
    name  = "log_connections"
    value = "1"
  }
  tags = var.tags
  lifecycle {
    create_before_destroy = true
  }
}

# The master password is generated and stored by RDS in Secrets Manager (never in Terraform
# state, never in a variable). The app and migration roles are created by the db-roles step.
resource "aws_db_instance" "this" {
  identifier     = var.name
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  db_name                       = "ekc"
  username                      = "ekc_admin"
  manage_master_user_password   = true
  master_user_secret_kms_key_id = var.kms_key_arn

  allocated_storage     = var.db_allocated_storage_gb
  max_allocated_storage = var.db_max_allocated_storage_gb
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = var.kms_key_arn

  multi_az               = var.db_multi_az
  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false
  parameter_group_name   = aws_db_parameter_group.this.name

  backup_retention_period             = var.db_backup_retention_days
  backup_window                       = "21:00-22:00"
  maintenance_window                  = "sun:22:30-sun:23:30"
  copy_tags_to_snapshot               = true
  deletion_protection                 = var.deletion_protection
  skip_final_snapshot                 = !var.deletion_protection
  final_snapshot_identifier           = var.deletion_protection ? "${var.name}-final" : null
  auto_minor_version_upgrade          = true
  iam_database_authentication_enabled = true

  performance_insights_enabled    = true
  performance_insights_kms_key_id = var.kms_key_arn
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  monitoring_interval             = 60
  monitoring_role_arn             = aws_iam_role.rds_monitoring.arn

  tags = var.tags
}

data "aws_iam_policy_document" "rds_monitoring_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["monitoring.rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "rds_monitoring" {
  name               = "${var.name}-rds-monitoring"
  assume_role_policy = data.aws_iam_policy_document.rds_monitoring_assume.json
  managed_policy_arns = [
    "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole",
  ]
  tags = var.tags
}

# ---------------------------------------------------------------- Redis
resource "aws_elasticache_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.private_subnet_ids
  tags       = var.tags
}

# The AUTH token is the one secret Terraform must generate itself. It lands in state, so the
# state backend MUST be encrypted and access-controlled (see backend.tf and docs).
resource "random_password" "redis" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "redis" {
  name       = "${var.name}/redis-auth"
  kms_key_id = var.kms_key_arn
  tags       = var.tags
}

resource "aws_secretsmanager_secret_version" "redis" {
  secret_id     = aws_secretsmanager_secret.redis.id
  secret_string = random_password.redis.result
}

resource "aws_elasticache_replication_group" "this" {
  replication_group_id = var.name
  description          = "${var.name} Redis (rate limits, login throttle, breakers)"
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = var.redis_node_type
  port                 = 6379

  num_cache_clusters         = 1 + var.redis_replicas_per_group
  automatic_failover_enabled = var.redis_replicas_per_group > 0
  multi_az_enabled           = var.redis_replicas_per_group > 0

  subnet_group_name  = aws_elasticache_subnet_group.this.name
  security_group_ids = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true
  kms_key_id                 = var.kms_key_arn
  transit_encryption_enabled = true
  auth_token                 = random_password.redis.result

  snapshot_retention_limit   = 3
  auto_minor_version_upgrade = true
  apply_immediately          = false
  tags                       = var.tags
}

# ---------------------------------------------------------------- Kafka (MSK)
resource "aws_cloudwatch_log_group" "kafka" {
  name              = "/${var.name}/msk"
  retention_in_days = 90
  kms_key_id        = var.kms_key_arn
  tags              = var.tags
}

resource "aws_msk_configuration" "this" {
  name           = "${var.name}-kafka"
  kafka_versions = [var.kafka_version]
  # No topic auto-creation: topics are created by the topics-init job with explicit settings.
  server_properties = <<-PROPS
    auto.create.topics.enable=false
    default.replication.factor=3
    min.insync.replicas=2
    num.partitions=6
    unclean.leader.election.enable=false
    delete.topic.enable=false
  PROPS
}

resource "aws_msk_cluster" "this" {
  cluster_name           = var.name
  kafka_version          = var.kafka_version
  number_of_broker_nodes = length(var.private_subnet_ids)

  broker_node_group_info {
    instance_type   = var.kafka_instance_type
    client_subnets  = var.private_subnet_ids
    security_groups = [aws_security_group.kafka.id]
    storage_info {
      ebs_storage_info {
        volume_size = var.kafka_ebs_gb
      }
    }
  }

  client_authentication {
    unauthenticated = false
    sasl {
      scram = true
    }
  }

  encryption_info {
    encryption_at_rest_kms_key_arn = var.kms_key_arn
    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
  }

  configuration_info {
    arn      = aws_msk_configuration.this.arn
    revision = aws_msk_configuration.this.latest_revision
  }

  logging_info {
    broker_logs {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.kafka.name
      }
    }
  }

  open_monitoring {
    prometheus {
      jmx_exporter {
        enabled_in_broker = true
      }
      node_exporter {
        enabled_in_broker = true
      }
    }
  }

  tags = var.tags
}

# SCRAM credentials for the application. MSK requires the secret name to start with
# "AmazonMSK_" and the secret to use a customer-managed key. The password lands in state
# (same caveat as the Redis token); rotate it by tainting random_password.kafka.
resource "random_password" "kafka" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "kafka" {
  name       = "AmazonMSK_${var.name}_app"
  kms_key_id = var.kms_key_arn
  tags       = var.tags
}

resource "aws_secretsmanager_secret_version" "kafka" {
  secret_id = aws_secretsmanager_secret.kafka.id
  secret_string = jsonencode({
    username = "ekc-app"
    password = random_password.kafka.result
  })
}

resource "aws_msk_scram_secret_association" "this" {
  cluster_arn     = aws_msk_cluster.this.arn
  secret_arn_list = [aws_secretsmanager_secret.kafka.arn]
  depends_on      = [aws_secretsmanager_secret_version.kafka]
}

output "rds_endpoint" {
  value = aws_db_instance.this.address
}

output "rds_master_secret_arn" {
  description = "Secrets Manager ARN holding the generated master credentials."
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}

output "redis_primary_endpoint" {
  value = aws_elasticache_replication_group.this.primary_endpoint_address
}

output "redis_auth_secret_arn" {
  value = aws_secretsmanager_secret.redis.arn
}

output "kafka_bootstrap_brokers_sasl_scram" {
  value = aws_msk_cluster.this.bootstrap_brokers_sasl_scram
}

output "kafka_secret_arn" {
  value = aws_secretsmanager_secret.kafka.arn
}
