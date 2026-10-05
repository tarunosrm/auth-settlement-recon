output "resource_group_name" {
  value = azurerm_resource_group.main.name
}

output "key_vault_name" {
  value = azurerm_key_vault.main.name
}

output "databricks_workspace_url" {
  value = azurerm_databricks_workspace.main.workspace_url
}