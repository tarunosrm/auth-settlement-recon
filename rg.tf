resource "azurerm_resource_group" "main" {
  name     = "rg-${var.project}-portfolio"
  location = var.location
  tags     = var.tags
}