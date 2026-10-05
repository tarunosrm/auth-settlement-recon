resource "azurerm_eventhub_namespace" "main" {
  name                = "ehn-recon-${random_string.suffix.result}" # reuses Phase 0 suffix
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "Standard"
  capacity            = 1
  tags                = var.tags
}

resource "azurerm_eventhub" "auth" {
  name              = "auth-events"
  namespace_id      = azurerm_eventhub_namespace.main.id
  partition_count   = 2
  message_retention = 1 # days
}

resource "azurerm_eventhub_consumer_group" "recon" {
  name                = "cg-recon"
  namespace_name      = azurerm_eventhub_namespace.main.name
  eventhub_name       = azurerm_eventhub.auth.name
  resource_group_name = azurerm_resource_group.main.name
}

# least privilege: producer credential can Send, nothing else
resource "azurerm_eventhub_authorization_rule" "send" {
  name                = "send-only"
  namespace_name      = azurerm_eventhub_namespace.main.name
  eventhub_name       = azurerm_eventhub.auth.name
  resource_group_name = azurerm_resource_group.main.name
  send                = true
  listen              = false
  manage              = false
}

resource "azurerm_eventhub_authorization_rule" "listen" {
  name                = "listen-only"
  namespace_name      = azurerm_eventhub_namespace.main.name
  eventhub_name       = azurerm_eventhub.auth.name
  resource_group_name = azurerm_resource_group.main.name
  listen              = true
  send                = false
  manage              = false
}

resource "azurerm_eventhub_consumer_group" "stream" {
  name                = "cg-stream"
  namespace_name      = azurerm_eventhub_namespace.main.name
  eventhub_name       = azurerm_eventhub.auth.name
  resource_group_name = azurerm_resource_group.main.name
}