output "registry" {
  description = "Models registered on the gateway (also stored in APIM as model-meta-<model>)."
  value       = local.registry
}

output "gateway_url" {
  description = "AI gateway base URL."
  value       = data.azurerm_api_management.this.gateway_url
}

output "eval_projects" {
  description = "Platform evaluation project per hosting account: endpoint, and the managed identity to grant Cognitive Services OpenAI User on the account."
  value = {
    for a, p in azapi_resource.eval_project : a => {
      endpoint     = "https://${a}.services.ai.azure.com/api/projects/${p.name}"
      principal_id = try(p.identity[0].principal_id, null)
    }
  }
}
