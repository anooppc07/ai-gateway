"""Decision records: after a deployment is applied, write one record per changed model to immutable storage.

A record says what was decided, by whom, on which change, and that it is in effect: the merged pull request (title,
commit, approvers on the approved commit, who merged it), the model's lifecycle state, version and tier after the
change, the run that applied it, and a SHA-256 of each file of the model folder (the evidence of what was approved).
It is written only after apply and smoke test succeed, so a record always means "in effect".

  record.py build <out_dir>      writes the records as JSON files into <out_dir> and prints their blob names
  record.py upload <out_dir>     uploads them (Azure CLI, signed in with the pipeline's OIDC identity: no keys)

Reads ENVIRONMENT, GITHUB_REPOSITORY, GITHUB_SHA, BEFORE_SHA (empty for a tag or manual run: one deployment record),
RUN_URL, GH_TOKEN; upload also RECORDS_STORAGE_ACCOUNT and RECORDS_CONTAINER (default records).

Storage: a container with a time-based immutability policy (WORM). Blobs can't be changed or deleted until the
retention ends, and the names include time and commit, so an upload never overwrites.
"""
import datetime
import glob
import hashlib
import json
import os
import subprocess
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNTS = "gateway/foundry-accounts"


def sh(*cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, cwd=ROOT).stdout


def api(path):
    return json.loads(sh("gh", "api", path))


def model_folders(before, sha):
    """Model folders changed between two commits; all of them when there is no previous commit."""
    if before and set(before) != {"0"}:
        names = sh("git", "diff", "--name-only", before, sha, "--", ACCOUNTS).split()
        return sorted({"/".join(n.split("/")[:5]) for n in names if n.count("/") >= 5 and n.split("/")[3] == "models"})
    return sorted(os.path.relpath(d, ROOT) for d in glob.glob(os.path.join(ROOT, ACCOUNTS, "*", "models", "*")))


def load(path):
    f = os.path.join(ROOT, path)
    if not os.path.exists(f):
        return None
    with open(f, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def old_value(before, path):
    if not before or set(before) == {"0"}:
        return None
    try:
        return yaml.safe_load(sh("git", "show", f"{before}:{path}")) or {}
    except subprocess.CalledProcessError:
        return None


def pull_request(repo, sha):
    """The merged pull request that brought this commit to main, with its approvals on the approved commit."""
    prs = [p for p in api(f"repos/{repo}/commits/{sha}/pulls") if p.get("merged_at")]
    if not prs:
        return None
    pr = api(f"repos/{repo}/pulls/{prs[0]['number']}")
    latest = {}
    for r in api(f"repos/{repo}/pulls/{pr['number']}/reviews?per_page=100"):
        if r["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[r["user"]["login"]] = r
    approved = [{"by": u, "at": r["submitted_at"], "commit": r["commit_id"]}
                for u, r in sorted(latest.items()) if r["state"] == "APPROVED" and r["commit_id"] == pr["head"]["sha"]]
    return {
        "number": pr["number"], "url": pr["html_url"], "title": pr["title"], "author": pr["user"]["login"],
        "approved_commit": pr["head"]["sha"], "approved_by": approved,
        "merged_by": (pr.get("merged_by") or {}).get("login"), "merged_at": pr["merged_at"],
        "merge_commit": pr["merge_commit_sha"],
        "description_sha256": hashlib.sha256((pr.get("body") or "").encode()).hexdigest(),
    }


def digest(folder):
    out = {}
    for f in sorted(glob.glob(os.path.join(ROOT, folder, "*"))):
        with open(f, "rb") as fh:
            out[os.path.relpath(f, ROOT)] = hashlib.sha256(fh.read()).hexdigest()
    return out


def build(out_dir):
    env, repo, sha = os.environ["ENVIRONMENT"], os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"]
    before = os.environ.get("BEFORE_SHA", "")
    now = datetime.datetime.now(datetime.timezone.utc)
    pr = pull_request(repo, sha)
    os.makedirs(out_dir, exist_ok=True)
    names = []
    for folder in model_folders(before, sha):
        name = folder.split("/")[-1]
        model, gov = load(f"{folder}/model.yaml"), load(f"{folder}/governance.yaml")
        old_gov = old_value(before, f"{folder}/governance.yaml")
        old_model = old_value(before, f"{folder}/model.yaml")
        state = ((gov or {}).get("lifecycle") or {}).get("state")
        old_state = ((old_gov or {}).get("lifecycle") or {}).get("state")
        if model is None:
            event = "removed"
        elif not before or set(before) == {"0"}:
            event = "deployed"
        elif old_model is None:
            event = "onboarded"
        elif state != old_state:
            event = f"lifecycle {old_state or '(none)'} -> {state}"
        else:
            event = "changed"
        record = {
            "schema": "model-decision-record/1",
            "event": event,
            "environment": env,
            "model": name,
            "version": ((model or old_model or {}).get("model") or {}).get("version"),
            "lifecycle": state,
            "governance": gov,
            "change": pr or {"note": "no merged pull request for this commit (tag or manual run)"},
            "commit": sha,
            "repository": repo,
            "applied": {"at": now.isoformat(timespec="seconds"), "run": os.environ.get("RUN_URL"),
                        "result": "terraform apply and smoke test succeeded"},
            "files_sha256": digest(folder),
        }
        blob = f"{env}/models/{name}/{now.strftime('%Y%m%dT%H%M%SZ')}-{sha[:7]}.json"
        path = os.path.join(out_dir, blob.replace("/", "__"))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
        names.append(blob)
        print(blob)
    if not names:
        print("No model changed: no record.", file=sys.stderr)


def upload(out_dir):
    account = os.environ.get("RECORDS_STORAGE_ACCOUNT")
    container = os.environ.get("RECORDS_CONTAINER") or "records"
    if not account:
        sys.exit("RECORDS_STORAGE_ACCOUNT isn't set (GitHub environment variable): decision records can't be written, "
                 "so the deployment isn't recorded. Set it up (README: Decision records) and re-run.")
    for f in sorted(glob.glob(os.path.join(out_dir, "*.json"))):
        blob = os.path.basename(f).replace("__", "/")
        subprocess.run(["az", "storage", "blob", "upload", "--auth-mode", "login", "--account-name", account,
                        "--container-name", container, "--name", blob, "--file", f, "--overwrite", "false",
                        "--only-show-errors", "--output", "none"], check=True)
        print(f"recorded: {container}/{blob}")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("build", "upload"):
        sys.exit(__doc__)
    build(sys.argv[2]) if sys.argv[1] == "build" else upload(sys.argv[2])
