output "cluster_name" {
  value = module.eks.cluster_name
}

output "app_role_arn" {
  description = "Annotate the Kubernetes ServiceAccount with eks.amazonaws.com/role-arn=<this>."
  value       = module.eks.app_role_arn
}

output "documents_bucket" {
  value = module.storage.documents_bucket
}

output "rds_endpoint" {
  value = module.data.rds_endpoint
}

output "redis_primary_endpoint" {
  value = module.data.redis_primary_endpoint
}

output "kafka_bootstrap_brokers" {
  description = "SASL/SCRAM over TLS (port 9096): set KAFKA_BOOTSTRAP_SERVERS to this."
  value       = module.data.kafka_bootstrap_brokers_sasl_scram
}

output "secret_arns" {
  description = "Secrets Manager ARNs to wire into the External Secrets operator."
  value = {
    rds   = module.data.rds_master_secret_arn
    redis = module.data.redis_auth_secret_arn
    kafka = module.data.kafka_secret_arn
  }
}
