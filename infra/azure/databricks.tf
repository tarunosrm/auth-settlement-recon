resource "azurerm_databricks_workspace" "main" {
  name                = "dbw-recon-${random_string.suffix.result}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "premium"
  tags                = var.tags
}

# ADR-011 note: KV-backed secret scopes authorize the CREATOR's vault
# permissions at scope creation; runtime reads go through the Databricks
# control plane governed by scope ACLs. No workspace-identity role
# assignment is part of this path. (See debug log D-09.)

resource "databricks_secret_scope" "eventhub" {
  name = "eventhub"
  keyvault_metadata {
    resource_id = azurerm_key_vault.main.id
    dns_name    = azurerm_key_vault.main.vault_uri
  }
}