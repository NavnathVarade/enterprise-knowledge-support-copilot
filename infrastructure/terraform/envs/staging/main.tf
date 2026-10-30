# Staging is smaller and cheaper but keeps the same security posture (encryption, private
# networking, TLS). It is NOT zone-resilient: one NAT gateway, single-AZ database.
locals {
  name = "ekc-staging"
  tags = {
    Project     = "enterprise-knowledge-copilot"
    Environment = "staging"
    Owner       = var.owner
    ManagedBy   = "terraform"
  }
}

module "storage" {
  source        = "../../modules/storage"
  name          = local.name
  force_destroy = true
  tags          = local.tags
}

module "network" {
  source             = "../../modules/network"
  name               = local.name
  az_count           = 2
  single_nat_gateway = true
  log_kms_key_arn    = module.storage.kms_key_arn
  tags               = local.tags
}

module "eks" {
  source               = "../../modules/eks"
  name                 = local.name
  vpc_id               = module.network.vpc_id
  private_subnet_ids   = module.network.private_subnet_ids
  kms_key_arn          = module.storage.kms_key_arn
  public_api_cidrs     = var.public_api_cidrs
  documents_bucket_arn = module.storage.documents_bucket_arn
  app_secret_arns      = [module.data.redis_auth_secret_arn, module.data.kafka_secret_arn, module.data.rds_master_secret_arn]
  node_instance_types  = ["m6i.large"]
  node_min             = 2
  node_max             = 4
  tags                 = local.tags
}

module "data" {
  source                      = "../../modules/data"
  name                        = local.name
  vpc_id                      = module.network.vpc_id
  private_subnet_ids          = module.network.private_subnet_ids
  client_security_group_id    = module.eks.node_security_group_id
  kms_key_arn                 = module.storage.kms_key_arn
  deletion_protection         = false
  db_instance_class           = "db.t4g.large"
  db_allocated_storage_gb     = 50
  db_max_allocated_storage_gb = 200
  db_multi_az                 = false
  db_backup_retention_days    = 7
  redis_node_type             = "cache.t4g.medium"
  redis_replicas_per_group    = 0
  kafka_instance_type         = "kafka.t3.small"
  kafka_ebs_gb                = 100
  tags                        = local.tags
}
