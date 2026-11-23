"""Smoke test for model platform deployment, run after terraform apply.

  model_smoke.py models                 every model in the gateway registry: deployment healthy, backend registered,
                                        kill switch state

Everything is read from Azure (the registry from APIM named values model-meta-*), not from Git: the test proves what
is live. A project's access to its models is tested by the project repository's onboarding pipeline.

Sign-in: Azure CLI (az login locally, azure/login in GitHub Actions). The identity needs Reader on APIM and the
hosting accounts.
"""
import glob
import json
import os
import sys

import requests
import yaml
from azure.identity import AzureCliCredential

from platform_env import environment

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ARM = "https://management.azure.com"
cred = AzureCliCredential(process_timeout=60)


def summary(text):
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(text + "\n\n")
    print(text)


def arm_get(path, api_version):
    token = cred.get_token(f"{ARM}/.default").token
    r = requests.get(f"{ARM}{path}", params={"api-version": api_version},
                     headers={"Authorization": f"Bearer {token}"}, timeout=60)
    return r


def apim_path(env):
    g = env["gateway"]
    return (f"/subscriptions/{g['subscription_id']}/resourceGroups/{g['resource_group']}"
            f"/providers/Microsoft.ApiManagement/service/{g['apim_name']}")


def named_values(env):
    r = arm_get(f"{apim_path(env)}/namedValues", "2024-05-01")
    if r.status_code != 200:
        sys.exit(f"Can't read the gateway's named values: {r.status_code} {r.text[:300]}")
    return {nv["name"]: nv["properties"].get("value") for nv in r.json()["value"]}


def registry(env):
    return {json.loads(v)["name"]: json.loads(v) for n, v in named_values(env).items() if n.startswith("model-meta-")}


def model_tests(model):
    found = glob.glob(os.path.join(ROOT, "gateway", "foundry-accounts", "*", "models", model, "tests.yaml"))
    if not found:
        return [{"input": "Reply with the single word OK.", "must_contain_any": ["OK"]}]
    with open(found[0], encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ----------------------------------------------------------------------------- models
def smoke_models(env):
    values = named_values(env)
    reg = {json.loads(v)["name"]: json.loads(v) for n, v in values.items() if n.startswith("model-meta-")}
    if not reg:
        sys.exit("The gateway registry is empty: no model-meta-* named values.")
    failed = []
    rows = ["| Model | Hosting account | Deployment | Backend | Kill switch |", "|---|---|---|---|---|"]
    for name, m in sorted(reg.items()):
        acct = next((a for a in glob.glob(os.path.join(ROOT, "gateway", "foundry-accounts", m["account"], "account.yaml"))), None)
        rg = yaml.safe_load(open(acct, encoding="utf-8"))["resource_group"] if acct else env["gateway"]["resource_group"]
        sub = env["gateway"]["subscription_id"]
        dep = arm_get(f"/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.CognitiveServices/accounts/"
                      f"{m['account']}/deployments/{m['deployment']}", "2025-06-01")
        dep_state = dep.json().get("properties", {}).get("provisioningState") if dep.status_code == 200 else f"HTTP {dep.status_code}"
        be = arm_get(f"{apim_path(env)}/backends/{m['backend_id']}", "2024-05-01")
        be_state = "registered" if be.status_code == 200 else f"HTTP {be.status_code}"
        ks = values.get(m["kill_switch"], "missing")
        rows.append(f"| {name} | {m['account']} ({m['region']}) | {dep_state} | {be_state} | {ks} |")
        # A retired model must be held at "retired" (else the gateway would still serve it); any other model is
        # active or suspended by operations.
        ks_ok = ks == "retired" if m.get("lifecycle") == "retired" else ks in ("active", "suspended")
        if dep_state != "Succeeded" or be_state != "registered" or not ks_ok:
            failed.append(name)
    summary("\n".join(rows))
    if failed:
        sys.exit(f"Smoke test failed for: {', '.join(failed)}")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "models":
        sys.exit("usage: model_smoke.py models   (environment: the job's variables, see platform_env.py; project model "
                 "access: the project repository's onboarding smoke test)")
    smoke_models(environment())
