terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
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

variable "kms_key_arn" {
  type = string
}

variable "kubernetes_version" {
  type    = string
  default = "1.29"
}

variable "public_api_cidrs" {
  type        = list(string)
  description = "CIDRs allowed to reach the public Kubernetes API endpoint. Empty = private endpoint only (recommended)."
  default     = []
}

variable "node_instance_types" {
  type    = list(string)
  default = ["m6i.xlarge"]
}

variable "node_min" {
  type    = number
  default = 3
}

variable "node_max" {
  type    = number
  default = 12
}

variable "documents_bucket_arn" {
  type = string
}

variable "app_secret_arns" {
  type        = list(string)
  description = "Secrets Manager ARNs the application's service account may read."
  default     = []
}

variable "k8s_namespace" {
  type    = string
  default = "ekc"
}

variable "k8s_service_account" {
  type    = string
  default = "ekc"
}

variable "tags" {
  type    = map(string)
  default = {}
}

# ---------------------------------------------------------------- control plane
data "aws_iam_policy_document" "cluster_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  name               = "${var.name}-eks-cluster"
  assume_role_policy = data.aws_iam_policy_document.cluster_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_cloudwatch_log_group" "cluster" {
  name              = "/aws/eks/${var.name}/cluster"
  retention_in_days = 90
  kms_key_id        = var.kms_key_arn
  tags              = var.tags
}

resource "aws_eks_cluster" "this" {
  name     = var.name
  version  = var.kubernetes_version
  role_arn = aws_iam_role.cluster.arn

  vpc_config {
    subnet_ids              = var.private_subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = length(var.public_api_cidrs) > 0
    public_access_cidrs     = length(var.public_api_cidrs) > 0 ? var.public_api_cidrs : null
  }

  # Kubernetes Secrets are envelope-encrypted with the environment key.
  encryption_config {
    resources = ["secrets"]
    provider {
      key_arn = var.kms_key_arn
    }
  }

  enabled_cluster_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]
  tags                      = var.tags

  depends_on = [aws_iam_role_policy_attachment.cluster, aws_cloudwatch_log_group.cluster]
}

# ---------------------------------------------------------------- nodes
data "aws_iam_policy_document" "node_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "${var.name}-eks-node"
  assume_role_policy = data.aws_iam_policy_document.node_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
  ])
  role       = aws_iam_role.node.name
  policy_arn = each.value
}

# Dedicated security group for nodes: this is the "client" the data services allow.
resource "aws_security_group" "node" {
  name_prefix = "${var.name}-node-"
  vpc_id      = var.vpc_id
  description = "EKS worker nodes and pods"
  tags        = merge(var.tags, { Name = "${var.name}-node" })
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_egress_rule" "node_all" {
  security_group_id = aws_security_group.node.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
  description       = "Egress is restricted per pod by Kubernetes NetworkPolicy"
}

resource "aws_vpc_security_group_ingress_rule" "node_self" {
  security_group_id            = aws_security_group.node.id
  referenced_security_group_id = aws_security_group.node.id
  ip_protocol                  = "-1"
}

resource "aws_vpc_security_group_ingress_rule" "node_from_cluster" {
  security_group_id            = aws_security_group.node.id
  referenced_security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  ip_protocol                  = "-1"
}

resource "aws_launch_template" "node" {
  name_prefix            = "${var.name}-node-"
  vpc_security_group_ids = [aws_security_group.node.id, aws_eks_cluster.this.vpc_config[0].cluster_security_group_id]

  # IMDSv2 only, hop limit 1: pods cannot reach the node's instance credentials.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size = 100
      volume_type = "gp3"
      encrypted   = true
      kms_key_id  = var.kms_key_arn
    }
  }
  tag_specifications {
    resource_type = "instance"
    tags          = merge(var.tags, { Name = "${var.name}-node" })
  }
}

resource "aws_eks_node_group" "this" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${var.name}-default"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.private_subnet_ids
  instance_types  = var.node_instance_types

  scaling_config {
    min_size     = var.node_min
    max_size     = var.node_max
    desired_size = var.node_min
  }
  update_config {
    max_unavailable = 1
  }
  launch_template {
    id      = aws_launch_template.node.id
    version = aws_launch_template.node.latest_version
  }

  # The cluster autoscaler / Karpenter owns desired_size after creation.
  lifecycle {
    ignore_changes = [scaling_config[0].desired_size]
  }
  tags       = var.tags
  depends_on = [aws_iam_role_policy_attachment.node]
}

# ---------------------------------------------------------------- IRSA for the application
data "tls_certificate" "oidc" {
  url = aws_eks_cluster.this.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "this" {
  url             = aws_eks_cluster.this.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.oidc.certificates[0].sha1_fingerprint]
  tags            = var.tags
}

locals {
  oidc_host = replace(aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://", "")
}

data "aws_iam_policy_document" "app_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.this.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = ["system:serviceaccount:${var.k8s_namespace}:${var.k8s_service_account}"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "app" {
  statement {
    sid       = "DocumentsObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"]
    resources = ["${var.documents_bucket_arn}/*"]
  }
  statement {
    sid       = "DocumentsList"
    actions   = ["s3:ListBucket"]
    resources = [var.documents_bucket_arn]
  }
  statement {
    sid       = "UseDataKey"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [var.kms_key_arn]
  }
  dynamic "statement" {
    for_each = length(var.app_secret_arns) > 0 ? [1] : []
    content {
      sid       = "ReadAppSecrets"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = var.app_secret_arns
    }
  }
}

resource "aws_iam_role" "app" {
  name               = "${var.name}-app"
  assume_role_policy = data.aws_iam_policy_document.app_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "app" {
  name   = "ekc-app"
  role   = aws_iam_role.app.id
  policy = data.aws_iam_policy_document.app.json
}

output "cluster_name" {
  value = aws_eks_cluster.this.name
}

output "node_security_group_id" {
  value = aws_security_group.node.id
}

output "app_role_arn" {
  description = "Annotate the Kubernetes ServiceAccount with eks.amazonaws.com/role-arn=<this>."
  value       = aws_iam_role.app.arn
}
