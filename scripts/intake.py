"""Model onboarding intake (Phase 1, step 1): checks a "Model onboarding request" issue and writes the model folder of
the pull request that the approvers review. The pull request review is the approval of record.

  intake.py check                 reads ISSUE_BODY; prints a JSON result {complete, refused, errors, labels, fields}
  intake.py render <out_dir>      reads ISSUE_BODY, ISSUE_NUMBER, REQUESTER; writes model.yaml and docs.md, tests.yaml
                                  (AI platform owns them) and governance.yaml (model risk owns it) into
                                  <out_dir>/<hosting account>/models/<model>/ and prints that folder
  intake.py pack <out_file>       reads ISSUE_BODY, ISSUE_NUMBER; when the request names the model it replaces, writes
                                  the evaluation pack from the template of the model type (else prints why not)
  intake.py summary               reads ISSUE_BODY, ISSUE_NUMBER, REQUESTER; prints the pull request description: a
                                  plain-language summary for approvers who don't read YAML

The issue form is .github/ISSUE_TEMPLATE/model-onboarding.yml. GitHub renders each field as "### <label>" followed by
the value ("_No response_" when empty).
"""
import glob
import json
import os
import re
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNTS = os.path.join(ROOT, "gateway", "foundry-accounts")
LABELS = {  # issue form label -> field
    "Model name": "model_name", "Model version": "model_version", "Provider": "provider", "Model type": "profile",
    "Capacity requested (thousand tokens per minute)": "capacity", "Model owner": "owner", "Model sponsor": "sponsor",
    "Business use case": "use_case", "Intended use and limitations": "intended_use", "Risk tier": "tier",
    "AICOE tier record ID": "tier_record", "AI Impact Assessment (AIIA) reference": "aiia",
    "Does the use case affect people (decisions about individuals)?": "affects_people",
    "Replaces model (optional)": "replaces",
}
OPTIONAL = {"replaces"}
TEMPLATES = os.path.join(ROOT, "gateway", "model-evals", "templates")
PROFILES = {"general-purpose": "openai", "reasoning": "openai", "embedding": "embeddings"}   # profile -> gateway api
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,62}$")
VERSION_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}|\d+(\.\d+)*)$")


def parse(body):
    fields, current = {}, None
    for line in (body or "").splitlines():
        m = re.match(r"^###\s+(.*?)\s*$", line)
        if m:
            current = LABELS.get(m.group(1))
            if current:
                fields[current] = ""
            continue
        if current:
            fields[current] += line + "\n"
    out = {}
    for k, v in fields.items():
        v = v.strip()
        out[k] = "" if v in ("", "_No response_", "None") else v
    return out


def hosting_account(environment="dev"):
    """The hosting account folder of the environment (the sandbox's dev stage has one)."""
    found = []
    for f in sorted(glob.glob(os.path.join(ACCOUNTS, "*", "account.yaml"))):
        with open(f, encoding="utf-8") as fh:
            if (yaml.safe_load(fh) or {}).get("environment") == environment:
                found.append(os.path.basename(os.path.dirname(f)))
    return found[0] if len(found) == 1 else None


def check(fields):
    errors, refused = [], False
    for label, key in LABELS.items():
        if key not in OPTIONAL and not fields.get(key):
            errors.append(f"**{label}** is empty.")
    name = fields.get("model_name", "").lower()
    if name and not NAME_RE.match(name):
        errors.append(f"**Model name** `{name}`: lowercase letters, digits, dots and hyphens only.")
    version = fields.get("model_version", "")
    if version and (version.lower() == "latest" or not VERSION_RE.match(version)):
        errors.append(f"**Model version** `{version}` must be an exact version (for example 2026-03-17), not latest.")
    if fields.get("profile") and fields["profile"] not in PROFILES:
        errors.append(f"**Model type** `{fields['profile']}` isn't supported yet.")
    cap = fields.get("capacity", "")
    if cap and (not cap.isdigit() or not 1 <= int(cap) <= 1000):
        errors.append(f"**Capacity** `{cap}`: a whole number of thousand tokens per minute, 1 to 1000.")
    tier = fields.get("tier", "")
    if tier.startswith("Tier 0"):
        refused = True
        errors.append("**Risk tier 0 is prohibited** (Standard): the request is refused and the project is paused.")
    owner = fields.get("owner", "").lstrip("@")
    if owner and not re.match(r"^[A-Za-z0-9-]{1,39}$", owner):
        errors.append(f"**Model owner** `{fields['owner']}`: a GitHub user name.")
    rep = fields.get("replaces", "").lower()
    if rep:
        gov_file = glob.glob(os.path.join(ACCOUNTS, "*", "models", rep, "governance.yaml"))
        if not gov_file:
            errors.append(f"**Replaces model** `{rep}` isn't a model on the platform.")
        else:
            with open(gov_file[0], encoding="utf-8") as fh:
                state = ((yaml.safe_load(fh) or {}).get("lifecycle") or {}).get("state")
            if state not in ("validated", "retiring"):
                errors.append(f"**Replaces model** `{rep}` is '{state}': only a model in use (validated) can be replaced.")
    account = hosting_account()
    if not account:
        errors.append("No single hosting account for the dev stage was found (platform setup).")
    elif name and os.path.isdir(os.path.join(ACCOUNTS, account, "models", name)):
        errors.append(f"Model `{name}` is already onboarded in `{account}`: a new version is a change to its model.yaml.")
    tier_n = tier.split()[1] if tier.startswith("Tier ") and len(tier.split()) > 1 else ""
    # Stage labels: where the request is, and who acts next.
    labels = ["intake", "intake:refused" if refused else ("intake:ready-for-review" if not errors else "intake:needs-info")]
    if tier_n:
        labels.append(f"tier:{tier_n}")
    if fields.get("profile") in PROFILES:
        labels.append(f"profile:{fields['profile']}")
    return {"complete": not errors, "refused": refused, "errors": errors, "labels": labels, "fields": fields,
            "model": name, "tier": tier_n, "account": account}


def render(fields, issue, out_dir, requester):
    r = check(fields)
    if not r["complete"]:
        sys.exit("The request isn't complete: " + " ".join(r["errors"]))
    name, account = r["model"], r["account"]
    folder = os.path.join(out_dir, account, "models", name)
    os.makedirs(folder, exist_ok=True)
    model = {
        "name": name,
        "api": PROFILES[fields["profile"]],
        "status": "active",
        "purpose": "serving",
        "profile": fields["profile"],
        "owner": fields["owner"].lstrip("@"),
        "model": {"format": "OpenAI", "name": name, "version": fields["model_version"]},
        "deployment": {"name": name, "sku": "GlobalStandard", "capacity": int(fields["capacity"]),
                       "version_upgrade": "NoAutoUpgrade", "existing": False},
        "guardrail": "Microsoft.DefaultV2",
        "docs_file": "docs.md",
        "tests_file": "tests.yaml",
    }
    governance = {
        "tier": int(r["tier"]),
        "tier_record": fields["tier_record"],
        "aiia": fields["aiia"],
        "affects_people": fields["affects_people"] == "Yes",
        "sponsor": fields["sponsor"],
        "intake_issue": int(issue),
        "requested_by": requester,
        "lifecycle": {"state": "onboarded"},   # onboarded -> evaluated -> validated -> retired, each by a merged PR
    }
    with open(os.path.join(folder, "governance.yaml"), "w", encoding="utf-8") as fh:
        fh.write(f"# Governance record of {name}, from intake request #{issue}. Owned by model risk (CODEOWNERS): changed\n"
                 "# only by a reviewed pull request. The requester can't approve it (check separation-of-duties).\n"
                 + yaml.safe_dump(governance, sort_keys=False))
    header = (f"# Onboarded from intake issue #{issue}. Owned by the AI platform (CODEOWNERS); the governance record is\n"
              "# governance.yaml, owned by model risk.\n")
    with open(os.path.join(folder, "model.yaml"), "w", encoding="utf-8") as fh:
        fh.write(header + yaml.safe_dump(model, sort_keys=False))
    with open(os.path.join(folder, "docs.md"), "w", encoding="utf-8") as fh:
        fh.write(f"# {name}\n\n{fields['use_case'].strip()}\n\n"
                 f"| | |\n|---|---|\n| Gateway model name | `{name}` |\n| Use in an agent | `<connection>/{name}` |\n"
                 f"| Model type | {fields['profile']} |\n| Version | {fields['model_version']}, pinned (no automatic upgrade) |\n"
                 f"| Risk tier | {r['tier']} |\n| Owner | {fields['owner']} |\n\n"
                 f"## Intended use and limitations\n\n{fields['intended_use'].strip()}\n")
    tests = ([{"input": "Reply with the single word OK.", "must_contain_any": ["OK"]}]
             if fields["profile"] != "embedding" else [{"embed": "hello world", "expect_dimensions_min": 256}])
    with open(os.path.join(folder, "tests.yaml"), "w", encoding="utf-8") as fh:
        fh.write("# Smoke tests run after deployment.\n" + yaml.safe_dump(tests, sort_keys=False))
    return folder


def pack(fields, issue, out_file):
    """The evaluation pack from the template of the model type, when the request names the model it replaces. Writes
    nothing (and prints why) when there's no model to compare with or no template for the type yet."""
    r = check(fields)
    rep = fields.get("replaces", "").lower()
    if not rep:
        print("no pack: the request names no model it replaces")
        return
    tmpl = os.path.join(TEMPLATES, f"{fields['profile']}.yaml")
    if not os.path.exists(tmpl):
        print(f"no pack: no evaluation template for model type {fields['profile']} yet")
        return
    with open(tmpl, encoding="utf-8") as fh:
        text = fh.read()
    lines = text.splitlines(keepends=True)
    while lines and lines[0].startswith("#"):   # the template's own header
        lines.pop(0)
    body = "".join(lines).replace("{candidate}", r["model"]).replace("{baseline}", rep)
    with open(out_file, "w", encoding="utf-8") as fh:
        fh.write(f"# Platform evaluation pack: {r['model']} against {rep}, the model it would replace. Written from the\n"
                 f"# {fields['profile']} template by intake #{issue}; model risk approves it in the onboarding pull request.\n"
                 "# A change to it is a new pack version (bump pack_version), approved by model risk.\n" + body)
    print(out_file)


def summary(fields, issue, requester):
    """Pull request description: what the approvers decide, in plain language, before any file."""
    r = check(fields)
    people = "**Yes**: decisions about individuals" if fields.get("affects_people") == "Yes" else "No"
    return f"""## What you are approving

Onboarding model **{r['model']}** (version {fields['model_version']}, {fields['provider']}), requested by @{requester} in #{issue}.

| | |
|---|---|
| Business use case | {one_line(fields['use_case'])} |
| Model type | {fields['profile']} |
| Risk tier | {r['tier']} (AICOE record {one_line(fields['tier_record'])}) |
| AI Impact Assessment | {one_line(fields['aiia'])} |
| Affects people | {people} |
| Sponsor / owner | {one_line(fields['sponsor'])} / @{fields['owner'].lstrip('@')} |
| Capacity | {fields['capacity']}k tokens per minute, version pinned (no automatic upgrade) |

**Intended use and limitations**

{fields['intended_use'].strip()}

**How it will be evaluated:** {evaluation_line(fields, r)}

**What merging does:** deploys the model in dev and registers it in the gateway. No project can use it until it has been
evaluated and approved for that project.

## Who approves

| Role | Reviews | File |
|---|---|---|
| Model risk | Tier, AICOE record, AIIA, people impact; the evaluation criteria | `governance.yaml`, `gateway/model-evals/{r['model']}.yaml` |
| AI platform | Capacity, version, deployment | `model.yaml`, `docs.md`, `tests.yaml` |

GitHub asks both (CODEOWNERS). The requester (@{requester}) can't approve: the check **separation-of-duties** ignores
their approval. An approval counts only for the version it was given on; if the request changes, approve again.

## How to decide

- **Approve:** *Files changed* → *Review changes* → **Approve** (also works in the GitHub Mobile app).
- **Send back:** *Review changes* → **Request changes**, with what's missing. The requester updates the request (#{issue});
  this pull request updates itself.
- **Reject:** close this pull request with a comment saying why. The request is closed as rejected.

Don't edit the files here: changes come from the request, so the record stays in one place.
"""


def evaluation_line(fields, r):
    rep = fields.get("replaces", "").lower()
    if not rep:
        return ("no model to compare with (a new capability): the platform team writes its evaluation pack before "
                "it is evaluated.")
    if not os.path.exists(os.path.join(TEMPLATES, f"{fields['profile']}.yaml")):
        return (f"against **{rep}**, but there's no evaluation template for {fields['profile']} models yet: the "
                "platform team writes its pack.")
    return (f"against **{rep}**, the model it would replace, with the {fields['profile']} pack in "
            f"`gateway/model-evals/{r['model']}.yaml` (in this pull request: approving it approves the criteria). "
            "The evaluation starts by itself once the model is deployed; a pass opens the validation pull request.")


def one_line(text):
    return " ".join((text or "").split()).replace("|", "\\|")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("check", "render", "summary", "pack"):
        sys.exit(__doc__)
    f = parse(os.environ.get("ISSUE_BODY", ""))
    if sys.argv[1] == "check":
        print(json.dumps(check(f)))
    elif sys.argv[1] == "pack":
        pack(f, os.environ["ISSUE_NUMBER"], sys.argv[2])
    elif sys.argv[1] == "render":
        print(render(f, os.environ["ISSUE_NUMBER"], sys.argv[2], os.environ["REQUESTER"]))
    else:
        print(summary(f, os.environ["ISSUE_NUMBER"], os.environ["REQUESTER"]))
