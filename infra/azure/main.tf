terraform {
  required_version = ">= 1.6.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    databricks = {
      source  = "databricks/databricks"
      version = "~> 1.60"
    }

  }

  backend "azurerm" {
    resource_group_name  = "rg-tfstate"
    storage_account_name = "sttfstate56838"
    container_name       = "tfstate"
    key                  = "card-recon.tfstate"
  }
}

provider "azurerm" {
  features {}
}


provider "databricks" {
  host      = azurerm_databricks_workspace.main.workspace_url
  auth_type = "azure-cli" # uses your az login — CI stays validate-only
}