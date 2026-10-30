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
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }

  # Partial backend configuration: bucket/key/region/lock table are supplied at init time so no
  # account-specific value lives in git:
  #   terraform init -backend-config=backend.hcl   (see backend.hcl.example)
  # The state bucket MUST be encrypted, versioned and private: state contains the generated
  # Redis/Kafka credentials.
  backend "s3" {
    encrypt = true
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = local.tags
  }
}
