locals {
  repo_root    = "${path.module}/../../.."
  accounts_dir = "${local.repo_root}/gateway/foundry-accounts"
  # The environment's coordinates, from the platform variables (GitHub environment variables).
  env = {
    gateway = {
      subscription_id  = var.apim_subscription_id
      resource_group   = var.apim_resource_group
      apim_name        = var.apim_name
      models_product   = var.models_product
      portal_anonymous = var.portal_anonymous
    }
  }

  # One entry per hosting account of this environment: the folder name is the Azure resource name.
  accounts = {
    for f in fileset(local.accounts_dir, "*/account.yaml") :
    dirname(f) => yamldecode(file("${local.accounts_dir}/${f}"))
    if yamldecode(file("${local.accounts_dir}/${f}")).environment == var.environment
  }

  # One entry per model: the folder name is the gateway model name, unique across all accounts.
  models = {
    for f in fileset(local.accounts_dir, "*/models/*/model.yaml") :
    basename(dirname(f)) => merge(yamldecode(file("${local.accounts_dir}/${f}")), {
      account = split("/", f)[0]
      # APIM resource names allow no dots: gpt-5.4-mini -> gpt-5-4-mini
      key        = replace(basename(dirname(f)), ".", "-")
      backend_id = replace("model-${split("/", f)[0]}-${yamldecode(file("${local.accounts_dir}/${f}")).deployment.name}", ".", "-")
    })
    if contains(keys(local.accounts), split("/", f)[0])
  }

  # Registry entry per model, stored in APIM (named value model-meta-<key>). The model-project-connection stack and
  # every project pipeline read the registry from APIM: it is the single source of truth.
  registry = {
    for name, m in local.models : name => {
      name         = name
      api          = m.api
      status       = m.status
      purpose      = try(m.purpose, "serving") # judge: scores evaluations; onboarding turns Prompt Shields off for it
      profile      = m.profile                 # model type: chooses the evaluation pack
      governance   = m.governance              # tier, AICOE tier record, AIIA, affects people, sponsor, intake issue
      format       = m.model.format
      model        = m.model.name
      version      = m.model.version
      account      = m.account
      region       = local.accounts[m.account].region
      deployment   = m.deployment.name
      capacity_tpm = m.deployment.capacity * 1000
      guardrail    = m.guardrail
      backend_id   = m.backend_id
      kill_switch  = "ks-model-${m.key}"
      # Model card summary for the Developer Portal: the first paragraph of docs.md after its title.
      summary = substr(trimspace(try(split("\n\n", trimspace(replace(file("${local.accounts_dir}/${m.account}/models/${name}/${m.docs_file}"), "/^#[^\n]*\n/", "")))[0], "")), 0, 400)
    }
  }

  # Developer Portal: catalog of every model on the product page (markdown). APIM limits a product description to
  # 1,000 characters, so the catalog has one short row per model; each model's full card is in its project model API
  # docs (the registry summary) and in docs.md.
  catalog = join("\n", concat([
    "Models served by the AI gateway. Agents use `<connection>/<model>`. To request one, add it to `models.allowed` in your `project.yaml`.",
    "",
    "| Model | Status | Region | TPM |",
    "|---|---|---|---|",
    ], [for n, r in local.registry : "| `${n}` | ${r.status} | ${r.region} | ${r.capacity_tpm} |"]
  ))
}
