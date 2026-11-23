## Validation: catches a wrong model definition before anything is changed

# Preconditions, not a check block: a check block only warns, so a wrong definition would still be applied.
resource "terraform_data" "model_definitions" {
  lifecycle {
    precondition {
      condition     = alltrue([for n, m in local.models : m.name == n])
      error_message = "model.yaml 'name' must equal its folder name (the gateway model name)."
    }
    precondition {
      condition     = alltrue([for n, m in local.models : can(regex("^[a-z0-9][a-z0-9.-]{0,62}$", n))])
      error_message = "Gateway model names: lowercase letters, digits, dots and hyphens, at most 63 characters."
    }
    precondition {
      condition     = alltrue([for n, m in local.models : contains(local.accounts[m.account].formats, m.model.format)])
      error_message = "A model's format must be one of its hosting account's formats (account.yaml)."
    }
    precondition {
      condition     = alltrue([for n, m in local.models : contains(["openai"], m.api) && contains(["active", "deprecated"], m.status)])
      error_message = "api must be 'openai'; status must be 'active' or 'deprecated'."
    }
    precondition {
      condition     = alltrue([for n, m in local.models : contains(["serving", "judge"], try(m.purpose, "serving"))])
      error_message = "purpose must be 'serving' (default) or 'judge' (scores evaluations only)."
    }
    # Phase 1 governance metadata, from the intake request: no model is onboarded without it.
    precondition {
      condition     = alltrue([for n, m in local.models : contains(["general-purpose", "reasoning", "embedding"], try(m.profile, ""))])
      error_message = "profile is required in model.yaml: general-purpose, reasoning or embedding (from the intake request)."
    }
    precondition {
      condition = alltrue([for n, m in local.models : (
        contains([1, 2, 3, 4], try(m.governance.tier, 0)) && try(m.governance.tier_record, "") != "" && try(m.governance.aiia, "") != ""
      )])
      error_message = "governance.yaml is required next to model.yaml, with tier (1-4; tier 0 is prohibited), tier_record and aiia (from the intake request)."
    }
    # Lifecycle state, in governance.yaml: each change is a merged, reviewed pull request; the registry shows it.
    precondition {
      condition     = alltrue([for n, m in local.models : contains(["onboarded", "evaluated", "validated", "retiring", "retired"], try(m.governance.lifecycle.state, ""))])
      error_message = "governance.yaml lifecycle.state is required: onboarded, evaluated, validated, retiring or retired."
    }
    # Retirement: a notice goes out only with an approved alternative. The replacement must be validated, as the
    # replacement of this model, by a platform evaluation run against the version of this model deployed now.
    precondition {
      condition = alltrue([for n, m in local.models : (
        !contains(["retiring", "retired"], m.governance.lifecycle.state) || (
          can(regex("^\\d{4}-\\d{2}-\\d{2}$", try(m.governance.lifecycle.retirement.date, ""))) &&
          try(local.models[m.governance.lifecycle.retirement.replacement].governance.lifecycle.state, "") == "validated" &&
          try(local.models[m.governance.lifecycle.retirement.replacement].governance.lifecycle.replaces, "") == n &&
          try(local.models[m.governance.lifecycle.retirement.replacement].governance.lifecycle.evaluation.baseline_version, "") == m.model.version
        )
      )])
      error_message = "A retiring or retired model needs lifecycle.retirement.date (YYYY-MM-DD) and a replacement that is validated, replaces it, and was evaluated against its current version (evaluation.baseline_version): ${join("; ", [for n, m in local.models : "${n} -> ${try(m.governance.lifecycle.retirement.replacement, "?")}" if contains(["retiring", "retired"], m.governance.lifecycle.state)])}."
    }
    # A model is retired only on or after its date (an earlier retirement first brings the date forward in a
    # reviewed pull request). A notice's future date is checked when it's given (retirement.py), not here: once the
    # date passes, deploys must keep working until the retirement pull request is merged.
    precondition {
      condition = alltrue([for n, m in local.models : (
        m.governance.lifecycle.state != "retired" || timecmp("${try(m.governance.lifecycle.retirement.date, "2999-01-01")}T00:00:00Z", plantimestamp()) <= 0
      )])
      error_message = "A model can be retired only on or after its retirement date."
    }
    precondition {
      condition     = alltrue([for n, m in local.models : !m.governance_inline])
      error_message = "Governance fields belong in governance.yaml (owned by model risk), not in model.yaml."
    }
    # Pinned versions: Azure must never swap the version that was evaluated and approved. A new version is
    # onboarded as its own model folder and evaluated before any project uses it.
    precondition {
      condition     = alltrue([for n, m in local.models : try(m.deployment.version_upgrade, "") == "NoAutoUpgrade"])
      error_message = "deployment.version_upgrade must be NoAutoUpgrade: model versions are pinned and changed only through onboarding."
    }
    precondition {
      condition     = alltrue([for n, m in local.models : can(regex("^[0-9A-Za-z.-]+$", tostring(m.model.version)))])
      error_message = "model.version must be an explicit version (for example \"2025-08-07\"), not empty."
    }
  }
}

## Model deployment in its hosting Foundry account

resource "azurerm_cognitive_deployment" "model" {
  for_each = local.models

  name                   = each.value.deployment.name
  cognitive_account_id   = data.azurerm_cognitive_account.hosting[each.value.account].id
  rai_policy_name        = each.value.guardrail
  version_upgrade_option = each.value.deployment.version_upgrade

  model {
    format  = each.value.model.format
    name    = each.value.model.name
    version = each.value.model.version
  }

  sku {
    name     = each.value.deployment.sku
    capacity = each.value.deployment.capacity
  }
}

# Deployments that existed before this pipeline are adopted, not recreated (deployment.existing: true).
import {
  for_each = { for n, m in local.models : n => m if try(m.deployment.existing, false) }
  to       = azurerm_cognitive_deployment.model[each.key]
  id       = "${data.azurerm_cognitive_account.hosting[each.value.account].id}/deployments/${each.value.deployment.name}"
}

## Gateway backend: the hosting account's Azure OpenAI endpoint. APIM authenticates with its managed identity
## (policy in the model-project-connection stack); its identity needs Foundry User on the hosting account.

resource "azurerm_api_management_backend" "model" {
  for_each = local.models

  name                = each.value.backend_id
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
  protocol            = "http"
  url                 = "${trimsuffix(data.azurerm_cognitive_account.hosting[each.value.account].endpoint, "/")}/openai"
  description         = "Model ${each.key}: deployment ${each.value.deployment.name} in ${each.value.account}"

  depends_on = [azurerm_cognitive_deployment.model]
}

## Registry entry (read by model-project-connection and project pipelines) and kill switch per model

resource "azurerm_api_management_named_value" "registry" {
  for_each = local.registry

  name                = "model-meta-${local.models[each.key].key}"
  display_name        = "model-meta-${local.models[each.key].key}"
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
  value               = jsonencode(each.value)
  secret              = false
  tags                = ["model-registry"]

  depends_on = [azurerm_api_management_backend.model]
}

resource "azurerm_api_management_named_value" "kill_switch" {
  for_each = local.models

  name                = "ks-model-${each.value.key}"
  display_name        = "ks-model-${each.value.key}"
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
  value               = "active"
  secret              = false
  tags                = ["kill-switch", "model"]

  lifecycle {
    # After creation the value belongs to operations (APIM > Named values, or the kill-switch workflow),
    # so an apply never re-activates a suspended model.
    ignore_changes = [value]
  }
}

## Product: every project's model API is added to it (model-project-connection stack)

resource "azurerm_api_management_product" "models" {
  product_id            = local.env.gateway.models_product
  display_name          = "Platform models"
  description           = local.catalog
  api_management_name   = data.azurerm_api_management.this.name
  resource_group_name   = data.azurerm_api_management.this.resource_group_name
  subscription_required = true
  approval_required     = true
  subscriptions_limit   = 100 # required with approval_required
  published             = true

  lifecycle {
    precondition {
      condition     = length(local.catalog) <= 1000
      error_message = "The model catalog (product description) is ${length(local.catalog)} characters; APIM allows at most 1,000. Shorten the catalog rows in locals.tf."
    }
  }
}

# Signed-in developers see the product (and its catalog) in the Developer Portal.
resource "azurerm_api_management_product_group" "developers" {
  product_id          = azurerm_api_management_product.models.product_id
  group_name          = "developers"
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
}

# Without portal sign-in (GitHub variable PORTAL_ANONYMOUS: true), anonymous visitors see the catalog too.
resource "azurerm_api_management_product_group" "guests" {
  count               = try(local.env.gateway.portal_anonymous, false) ? 1 : 0
  product_id          = azurerm_api_management_product.models.product_id
  group_name          = "guests"
  api_management_name = data.azurerm_api_management.this.name
  resource_group_name = data.azurerm_api_management.this.resource_group_name
}

# Platform evaluation project, one per hosting account: the platform team's own Foundry project, where the
# model-evaluation workflow evaluates a candidate model against the model it would replace. It sits in the account
# that hosts the models, so it reaches every deployment there directly (no connection, no project, nothing per
# model). Use cases never call it. Its managed identity needs Cognitive Services OpenAI User on the account, granted
# once (README, Platform evaluation).
resource "azapi_resource" "eval_project" {
  for_each = local.accounts

  type      = "Microsoft.CognitiveServices/accounts/projects@2025-06-01"
  name      = "platform-eval"
  parent_id = data.azurerm_cognitive_account.hosting[each.key].id
  location  = data.azurerm_cognitive_account.hosting[each.key].location

  identity {
    type = "SystemAssigned"
  }

  body = {
    properties = {
      displayName = "Platform model evaluation"
      description = "AI platform team: candidate models against the models they would replace. Not for use cases."
    }
  }
}

# A retired model is blocked at the gateway for every project: its kill switch is held at "retired" (the route
# fragment answers 410 with the replacement's name in the registry). Unlike an operator's suspension, this comes
# from Git: an apply sets it again, so a retired model is reactivated only by un-retiring it in a pull request.
resource "azapi_update_resource" "retired" {
  for_each = { for n, m in local.models : n => m if m.governance.lifecycle.state == "retired" }

  type        = "Microsoft.ApiManagement/service/namedValues@2024-05-01"
  resource_id = azurerm_api_management_named_value.kill_switch[each.key].id
  body = {
    properties = {
      value = "retired"
    }
  }
}
