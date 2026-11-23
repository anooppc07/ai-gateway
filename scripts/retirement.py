"""Model retirement: a notice with an approved replacement, then retirement on the date, each as a pull request.

  retirement.py notice <model> <date> <replacement> <reason> <out.md>
        sets the model's governance.yaml lifecycle to retiring with retirement {date, replacement, reason,
        requested_by}; writes the pull request description to out.md. Refused unless the replacement is validated,
        replaces this model, and was evaluated against the version of it deployed now (Terraform checks the same).
  retirement.py due
        prints the retiring models whose date has come
  retirement.py retire <model> <out.md>
        sets that model's lifecycle to retired and writes the pull request description, listing the projects that
        still use it

Projects that use a model: the gateway's named values model-limit-<project>-<model> (written by each project's
use-case onboarding), read with the Azure CLI (APIM_SUBSCRIPTION_ID, APIM_RESOURCE_GROUP, APIM_NAME).

The pull request is the decision: model risk (governance.yaml) approves it. After the merge the registry shows the
state; project repositories read it (model-adoption) and tell each project, with the replacement.
"""
import datetime
import glob
import json
import os
import subprocess
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNTS = os.path.join(ROOT, "gateway", "foundry-accounts")


def folder(name):
    found = glob.glob(os.path.join(ACCOUNTS, "*", "models", name))
    if len(found) != 1:
        sys.exit(f"Model {name} isn't onboarded (no folder gateway/foundry-accounts/*/models/{name}).")
    return found[0]


def read(name, file):
    with open(os.path.join(folder(name), file), encoding="utf-8") as fh:
        return fh.read()


def write_governance(name, gov):
    path = os.path.join(folder(name), "governance.yaml")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    header = ""
    for line in text.splitlines(keepends=True):   # keep the file's leading comment
        if not line.startswith("#"):
            break
        header += line
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header + yaml.safe_dump(gov, sort_keys=False))


def users(model):
    """Projects with an allocation on the model at the gateway: [(project, tokens per minute)]."""
    sub, rg, apim = (os.environ.get(k) for k in ("APIM_SUBSCRIPTION_ID", "APIM_RESOURCE_GROUP", "APIM_NAME"))
    if not (sub and rg and apim):
        return None
    url = (f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.ApiManagement/"
           f"service/{apim}/namedValues?api-version=2024-05-01&$filter=startswith(name,'model-limit-')&$top=100")
    out = json.loads(subprocess.run(["az", "rest", "--method", "get", "--url", url], check=True,
                                    capture_output=True, text=True).stdout)
    rows = []
    for nv in out.get("value", []):
        v = json.loads(nv["properties"].get("value") or "{}")
        if v.get("model") == model:
            rows.append((v.get("project"), int(v.get("tokens_per_minute") or 0)))
    return sorted(rows)


def users_table(model):
    rows = users(model)
    if rows is None:
        return "_(not read: no gateway coordinates)_"
    if not rows:
        return "None: no project has an allocation on it."
    return "| Project | Tokens per minute |\n|---|---|\n" + "\n".join(f"| {p} | {t:,} |" for p, t in rows)


def notice(model, date, replacement, reason, out):
    try:
        day = datetime.date.fromisoformat(date)
    except ValueError:
        sys.exit(f"Retirement date {date}: YYYY-MM-DD.")
    if day <= datetime.date.today():
        sys.exit(f"Retirement date {date} must be in the future: a notice gives projects time to move.")
    gov = yaml.safe_load(read(model, "governance.yaml")) or {}
    mdl = yaml.safe_load(read(model, "model.yaml")) or {}
    life = gov.get("lifecycle") or {}
    if life.get("state") != "validated":
        sys.exit(f"{model} is '{life.get('state')}': only a validated model in use is given a retirement notice.")
    rgov = yaml.safe_load(read(replacement, "governance.yaml")) or {}
    rlife = rgov.get("lifecycle") or {}
    ev = rlife.get("evaluation") or {}
    version = str(mdl["model"]["version"])
    problems = []
    if rlife.get("state") != "validated":
        problems.append(f"{replacement} is '{rlife.get('state')}', not validated")
    if rlife.get("replaces") != model:
        problems.append(f"{replacement} was validated as the replacement of '{rlife.get('replaces')}', not {model}")
    if str(ev.get("baseline_version", "")) != version:
        problems.append(f"{replacement}'s platform evaluation ran against {model} version "
                        f"'{ev.get('baseline_version', 'not recorded')}', but {version} is deployed: evaluate again")
    if problems:
        sys.exit("No notice without an approved alternative: " + "; ".join(problems) + ".")
    life = dict(life, state="retiring", retirement={
        "date": date, "replacement": replacement, "reason": reason,
        "requested_by": os.environ.get("GITHUB_ACTOR", "local")})
    gov["lifecycle"] = life
    write_governance(model, gov)
    days = (day - datetime.date.today()).days
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(f"""## What you are approving: a retirement notice

**{model}** (version {version}) retires on **{date}**, in {days} days. Its replacement is **{replacement}**.

| | |
|---|---|
| Reason | {reason} |
| Replacement | {replacement}: validated by model risk as the replacement of {model} |
| Platform evaluation | pack {ev.get('pack')}, {ev.get('queries')} queries, judge `{ev.get('judge')}`, against {model} {version} · [run]({ev.get('run')}) · evidence `{ev.get('record')}` |
| Requested by | @{os.environ.get('GITHUB_ACTOR', 'local')} (can't approve this: check separation-of-duties) |

**Projects that use {model} today** (allocations at the gateway):

{users_table(model)}

**What merging does:** {model} becomes `retiring` in the registry. Each project that uses it gets an issue in its own
repository with the date and the replacement, and a proposal to adopt {replacement} (its own agent evaluation runs as
soon as the team merges it), with reminders 30, 14, 7 and 1 days before. {model} keeps serving until the date.

**On the date** a pull request proposes retiring it, listing the projects still using it. Retiring it answers their
calls with 410 Gone; that is decided then, not now.
""")


def due_models():
    today = datetime.date.today()
    out = []
    for f in sorted(glob.glob(os.path.join(ACCOUNTS, "*", "models", "*", "governance.yaml"))):
        with open(f, encoding="utf-8") as fh:
            life = (yaml.safe_load(fh) or {}).get("lifecycle") or {}
        if life.get("state") == "retiring" and datetime.date.fromisoformat(str((life.get("retirement") or {}).get("date"))) <= today:
            out.append(os.path.basename(os.path.dirname(f)))
    return out


def retire(name, out):
    if name not in due_models():
        sys.exit(f"{name} isn't a retiring model whose date has come.")
    gov = yaml.safe_load(read(name, "governance.yaml")) or {}
    life = gov.get("lifecycle") or {}
    ret = life.get("retirement") or {}
    gov["lifecycle"] = dict(life, state="retired")
    write_governance(name, gov)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(f"""## What you are approving: retire {name}

{name} reached its retirement date ({ret.get('date')}). The notice went out with **{ret.get('replacement')}** as its
replacement. Reason: {ret.get('reason')}.

**Projects that still use {name}** (allocations at the gateway):

{users_table(name)}

**What merging does:** {name} becomes `retired`. The gateway answers every call to it with **410 Gone** (`MODEL_RETIRED`),
for every project, and an apply keeps it that way: it is reactivated only by un-retiring it in a pull request. Project
onboarding then refuses it in `models.allowed`, so each project removes it. The deployment itself is removed later, in
its own pull request (removing the model folder).

If a project above still serves agents on {name}, merging stops them. Talk to its owner first, or bring the date
forward or back in a reviewed pull request.
""")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "notice" and len(sys.argv) == 7:
        notice(*sys.argv[2:])
    elif len(sys.argv) == 2 and sys.argv[1] == "due":
        print("\n".join(due_models()))
    elif len(sys.argv) == 4 and sys.argv[1] == "retire":
        retire(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
