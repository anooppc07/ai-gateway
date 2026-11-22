"""Separation of duties on a pull request: the required check behind "who approved this change".

Branch protection already requires an approval from the code owners (CODEOWNERS). It can't tell who *asked* for a
change: an intake pull request is opened by the workflow bot, so GitHub would let the requester approve it. This check
closes that gap. It passes only if every file changed is approved, on the pull request's current commit, by one of its
code owners who is neither

  - the requester of the intake request (branch intake/<issue>-<model>: the author of issue <issue>),
  - the pull request author, nor
  - anyone who authored a commit in it.

Approvals given on an older commit don't count: a change after approval needs approval again.

  approval_check.py      reads GITHUB_REPOSITORY, PR_NUMBER, GH_TOKEN; writes a table to GITHUB_STEP_SUMMARY;
                         exits 1 when an owner's approval is missing, 2 when the check couldn't run

Teams in CODEOWNERS (@org/team) need a token that can read team membership; the workflow token can't, so the check
fails and says so rather than passing. The test bed's CODEOWNERS names users.
"""
import json
import os
import re
import subprocess
import sys

REPO = os.environ.get("GITHUB_REPOSITORY", "")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def api(path):
    out = subprocess.run(["gh", "api", "--paginate", "--slurp", path], check=True, capture_output=True, text=True).stdout
    pages = json.loads(out)
    if pages and isinstance(pages[0], list):
        return [x for page in pages for x in page]
    return pages[0] if pages else None


def glob_to_regex(pattern):
    """CODEOWNERS uses gitignore rules: '/x' anchors at the root, 'x/' matches a folder and everything in it, '*' stays
    within one path segment, '**' spans segments, and a pattern without '/' matches at any depth."""
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    p = pattern.lstrip("/")
    folder = p.endswith("/")
    p = p.rstrip("/")
    rx = ""
    i = 0
    while i < len(p):
        if p.startswith("**/", i):
            rx += "(?:.*/)?"
            i += 3
        elif p.startswith("**", i):
            rx += ".*"
            i += 2
        elif p[i] == "*":
            rx += "[^/]*"
            i += 1
        elif p[i] == "?":
            rx += "[^/]"
            i += 1
        else:
            rx += re.escape(p[i])
            i += 1
    prefix = "" if anchored else "(?:.*/)?"
    suffix = "/.*" if folder else "(?:/.*)?"
    return re.compile(f"^{prefix}{rx}{suffix}$")


def codeowners():
    for path in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        f = os.path.join(ROOT, path)
        if os.path.exists(f):
            rules = []
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.split("#", 1)[0].strip()
                    if line:
                        pattern, *owners = line.split()
                        rules.append((glob_to_regex(pattern), pattern, [o.lstrip("@").lower() for o in owners]))
            return rules
    sys.exit("No CODEOWNERS file: nobody is required to approve, so the check can't pass.")


def owners_of(path, rules):
    found = (None, [])
    for rx, pattern, owners in rules:   # the last matching rule wins, as in GitHub
        if rx.match(path):
            found = (pattern, owners)
    return found


def main():
    n = os.environ["PR_NUMBER"]
    pr = api(f"repos/{REPO}/pulls/{n}")
    head = pr["head"]["sha"]
    excluded = {pr["user"]["login"].lower()}
    m = re.match(r"^intake/(\d+)-", pr["head"]["ref"])
    requester = None
    if m:
        requester = api(f"repos/{REPO}/issues/{m.group(1)}")["user"]["login"]
        excluded.add(requester.lower())
    for c in api(f"repos/{REPO}/pulls/{n}/commits"):
        if c.get("author") and c["author"].get("type") != "Bot":
            excluded.add(c["author"]["login"].lower())

    # Latest decision of each reviewer; only an approval on the current commit counts.
    latest = {}
    for r in api(f"repos/{REPO}/pulls/{n}/reviews"):
        if r["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[r["user"]["login"].lower()] = r
    approvers = {u for u, r in latest.items() if r["state"] == "APPROVED" and r["commit_id"] == head}
    stale = {u for u, r in latest.items() if r["state"] == "APPROVED" and r["commit_id"] != head}
    ignored = approvers & excluded
    counted = approvers - excluded

    rules = codeowners()
    groups = {}   # owners rule -> files
    for f in api(f"repos/{REPO}/pulls/{n}/files"):
        pattern, owners = owners_of(f["filename"], rules)
        groups.setdefault((pattern, tuple(owners)), []).append(f["filename"])

    ok, rows = True, []
    for (pattern, owners), files in groups.items():
        if not owners:
            rows.append((pattern or "(no owner)", "-", "no owner in CODEOWNERS", files))
            ok = False
            continue
        if any("/" in o for o in owners):
            rows.append((pattern, ", ".join("@" + o for o in owners), "team owners can't be checked with this token", files))
            ok = False
            continue
        by = sorted(set(owners) & counted)
        rows.append((pattern, ", ".join("@" + o for o in owners),
                     "approved by " + ", ".join("@" + u for u in by) if by else "**waiting for approval**", files))
        ok = ok and bool(by)

    lines = [f"### Separation of duties: {'passed' if ok else 'not yet'}", "",
             f"Commit `{head[:7]}`." + (f" Requester: @{requester}." if requester else ""), "",
             "| Owners rule | Owners | Decision | Files |", "|---|---|---|---|"]
    for pattern, owners, decision, files in rows:
        lines.append(f"| `{pattern}` | {owners} | {decision} | {', '.join('`' + f + '`' for f in files)} |")
    if ignored:
        lines += ["", "Not counted (requester, author or committer of this change): "
                  + ", ".join("@" + u for u in sorted(ignored)) + "."]
    if stale:
        lines += ["", "Given on an older commit, so approve again: " + ", ".join("@" + u for u in sorted(stale)) + "."]
    text = "\n".join(lines) + "\n"
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(text)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:   # an API or parsing error: exit 2, reported as "error", not as "waiting for approval"
        import traceback
        traceback.print_exc()
        sys.exit(2)
