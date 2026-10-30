variable "region" {
  type    = string
  default = "us-east-1"
}

variable "owner" {
  type        = string
  description = "Team or cost-centre tag value."
}

variable "public_api_cidrs" {
  type        = list(string)
  description = "CIDRs allowed to reach the public Kubernetes API. Empty (default) = private only."
  default     = []
  validation {
    condition     = !contains(var.public_api_cidrs, "0.0.0.0/0")
    error_message = "Do not expose the Kubernetes API to 0.0.0.0/0."
  }
}
