locals {
  name = "ekc-production"
  tags = {
    Project     = "enterprise-knowledge-copilot"
    Environment = "production"
    Owner       = var.owner
    ManagedBy   = "terraform"
  }
}

module "storage" {
  source        = "../../modules/storage"
  name          = local.name
  force_destroy = false
  tags          = local.tags
}

module "network" {
  source             = "../../modules/network"
  name               = local.name
  az_count           = 3
  single_nat_gateway = false
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
  node_min             = 3
  node_max             = 12
  tags                 = local.tags
}

module "data" {
  source                   = "../../modules/data"
  name                     = local.name
  vpc_id                   = module.network.vpc_id
  private_subnet_ids       = module.network.private_subnet_ids
  client_security_group_id = module.eks.node_security_group_id
  kms_key_arn              = module.storage.kms_key_arn
  deletion_protection      = true
  db_multi_az              = true
  db_backup_retention_days = 14
  redis_replicas_per_group = 1
  tags                     = local.tags
}
