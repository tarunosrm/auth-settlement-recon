variable "location" {
  description = "Azure region for all resources"
  type        = string
  default     = "centralindia"
}

variable "project" {
  description = "Short prefix used in resource names"
  type        = string
  default     = "recon"
}

variable "tags" {
  description = "Tags applied to every resource"
  type        = map(string)
  default = {
    project     = "card-auth-settlement-recon"
    managed_by  = "terraform"
    environment = "dev"
  }
}