"""Platform model evaluation (Phase 1 step 3, test bed G4): a candidate model against the model it would replace, and
the pull request that asks model risk to validate it.

  model_eval.py run <pack.yaml> <out.json>   evaluate; writes the evaluation record; exits 1 on a failed verdict
  model_eval.py propose <pack.yaml> <record.json> <blob>
                                             on a pass: sets the candidate's governance.yaml lifecycle to validated,
                                             with replaces: <baseline> and the evaluation evidence (record blob
                                             <blob> in the immutable store); prints the pull request description

run: in the platform evaluation Foundry project (GitHub variables EVAL_PROJECT_ENDPOINT, EVAL_CONNECTION). The target
is the model itself (Foundry azure_ai_model target) through the project's gateway connection, <connection>/<model>,
so calls go through the AI gateway like any project's. Both models answer the same queries; the same judge scores
both. The verdict is a coverage check, then four checks, all required:
  floors         candidate pass rate per evaluator >= its floor
  hard stops     listed evaluators pass every item
  no regression  candidate pass rate >= baseline pass rate - margin (non-inferiority), per evaluator
  budget         p95 target latency, tokens per answer and cost per answer (as the Foundry report shows them), each
                 within the pack's ratio of the baseline's
Coverage: per evaluator and model, at least min_coverage of the queries scored (default 1.0).
The judge: the pack's judge_model, else EVAL_JUDGE_MODEL (a gateway model name).

The candidate must be onboarded (governance.yaml lifecycle.state onboarded or evaluated). The evaluation itself
decides nothing: a pass opens a pull request, and model risk's approval of it is the validation.
"""
import datetime
import glob
import hashlib
import json
import os
import sys
import time

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNTS = os.path.join(ROOT, "gateway", "foundry-accounts")
EVALUATORS = {"relevance", "coherence", "fluency", "intent_resolution"}
ITEM_SCHEMA = {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}}
EVALUABLE = {"onboarded", "evaluated"}


def summary(text):
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(text + "\n\n")
    print(text)


def model_folder(name):
    found = glob.glob(os.path.join(ACCOUNTS, "*", "models", name))
    if len(found) != 1:
        sys.exit(f"Model {name} isn't onboarded (no single folder gateway/foundry-accounts/*/models/{name}).")
    return found[0]


def governance(name):
    with open(os.path.join(model_folder(name), "governance.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_pack(path):
    with open(path, encoding="utf-8") as fh:
        pack = yaml.safe_load(fh)
    for key in ("candidate", "baseline", "pack_version", "evaluators", "dataset"):
        if key not in pack:
            sys.exit(f"{path}: '{key}' is required")
    unknown = set(pack["evaluators"]) - EVALUATORS
    if unknown:
        sys.exit(f"{path}: unknown evaluators {sorted(unknown)}; use {sorted(EVALUATORS)}")
    if not 0 < float(pack.get("min_coverage", 1.0)) <= 1:
        sys.exit(f"{path}: min_coverage is the share of queries that must be scored, (0-1]")
    if not set(pack.get("hard_stops") or []) <= set(pack["evaluators"]):
        sys.exit(f"{path}: hard_stops must be listed under evaluators")
    pack["judge"] = pack.get("judge_model") or os.environ.get("EVAL_JUDGE_MODEL") or sys.exit(
        "No judge: set judge_model in the pack or the GitHub variable EVAL_JUDGE_MODEL (a gateway model name)")
    pack["dataset_sha"] = hashlib.sha256(json.dumps(pack["dataset"], sort_keys=True).encode()).hexdigest()[:12]
    pack["path"] = os.path.relpath(os.path.abspath(path), ROOT)
    return pack


def criteria(pack, connection):
    return [{"type": "azure_ai_evaluator", "name": e, "evaluator_name": f"builtin.{e}",
             "initialization_parameters": {"deployment_name": f"{connection}/{pack['judge']}"},
             "data_mapping": {"query": "{{item.query}}", "response": "{{sample.output_text}}"}}
            for e in pack["evaluators"]]


def evaluation(oai, pack, connection):
    """One Foundry evaluation per candidate, pack version and judge, so baseline and candidate runs sit side by side."""
    tc = criteria(pack, connection)
    key = hashlib.sha256(json.dumps([tc, ITEM_SCHEMA, "model-target"], sort_keys=True).encode()).hexdigest()[:12]
    for e in oai.evals.list(limit=100):
        if (e.metadata or {}).get("model_eval_key") == key:
            return e
    return oai.evals.create(
        name=f"platform evaluation: {pack['candidate']} vs {pack['baseline']} (pack v{pack['pack_version']})",
        metadata={"model_eval_key": key, "candidate": pack["candidate"], "baseline": pack["baseline"],
                  "pack_version": str(pack["pack_version"]), "judge": pack["judge"], "pipeline": "ai-gateway model-evaluation"},
        data_source_config={"type": "custom", "include_sample_schema": True, "item_schema": ITEM_SCHEMA},
        testing_criteria=tc)


def run_model(oai, definition, pack, connection, model, role):
    run = oai.evals.runs.create(
        eval_id=definition.id, name=f"{role} {model}",
        metadata={"model": model, "role": role, "pack_version": str(pack["pack_version"]),
                  "dataset_sha": pack["dataset_sha"], "judge": pack["judge"],
                  "git_commit": os.environ.get("GITHUB_SHA", "local")[:40], "pipeline": "ai-gateway model-evaluation"},
        data_source={"type": "azure_ai_target_completions",
                     "source": {"type": "file_content", "content": [{"item": {"query": d["query"]}} for d in pack["dataset"]]},
                     "input_messages": {"type": "template", "template": [{
                         "type": "message", "role": "user", "content": {"type": "input_text", "text": "{{item.query}}"}}]},
                     "target": {"type": "azure_ai_model", "model": f"{connection}/{model}"}})
    print(f"{role} {model}: evaluation running ({len(pack['dataset'])} queries, target {connection}/{model})")
    return run


def wait(oai, definition, run):
    end = time.time() + 2400
    while time.time() < end:
        r = oai.evals.runs.retrieve(run_id=run.id, eval_id=definition.id)
        if r.status in ("completed", "failed", "canceled"):
            return r
        time.sleep(15)
    sys.exit(f"evaluation run {run.id} still running after 40 minutes")


def as_dict(x):
    if x is None:
        return {}
    return x if isinstance(x, dict) else (x.model_dump() if hasattr(x, "model_dump") else vars(x))


def errors(oai, definition, r, limit=3):
    """Why items weren't scored, or why the run failed: the first few errors."""
    out = []
    err = as_dict(getattr(r, "error", None))
    if err.get("message"):
        out.append(f"run: {err.get('code', '')} {err['message']}"[:300])
    try:
        for item in oai.evals.runs.output_items.list(run_id=r.id, eval_id=definition.id, limit=100):
            sample = as_dict(getattr(item, "sample", None))
            msg = (as_dict(sample.get("error")) or {}).get("message")
            if not msg:
                for res in (getattr(item, "results", None) or []):
                    d = as_dict(res)
                    if d.get("passed") is None:
                        e = as_dict((as_dict(d.get("sample")) or {}).get("error"))
                        msg = f"{d.get('name', '?')}: " + (e.get("message") or json.dumps(d, default=str))[:400]
                        break
            if msg:
                out.append(str(msg)[:300])
            if len(out) >= limit:
                break
    except Exception as e:  # diagnostics only
        out.append(f"(could not list output items: {e})")
    return out


def rates(r):
    out = {}
    for pc in (getattr(r, "per_testing_criteria_results", None) or []):
        total = pc.passed + pc.failed
        out[pc.testing_criteria] = {"passed": pc.passed, "total": total, "rate": round(pc.passed / total, 3) if total else 0.0}
    return out


def foundry_budget(oai, definition, r, pack, model):
    """Budget from the scored run, as the Foundry report shows it: target latency (p50/p95) and tokens per answer."""
    d = as_dict(r)
    lat = as_dict(as_dict(d.get("latency")).get("target"))
    tokens = []
    try:
        for item in oai.evals.runs.output_items.list(run_id=r.id, eval_id=definition.id, limit=100):
            u = as_dict(as_dict(getattr(item, "sample", None)).get("usage"))
            t = u.get("total_tokens") or ((u.get("prompt_tokens") or 0) + (u.get("completion_tokens") or 0))
            if t:
                tokens.append(t)
    except Exception as e:
        print(f"  (token usage not read from output items: {e})")
    cost = as_dict(as_dict(d.get("estimated_cost")).get("target"))
    price = ((pack.get("budget") or {}).get("prices") or {}).get(model)
    avg_tokens = round(sum(tokens) / len(tokens), 1) if tokens else None
    if cost.get("estimated_cost") is not None and lat.get("sample_count"):
        cost_avg = round(float(cost["estimated_cost"]) / int(lat["sample_count"]), 6)
    elif price is not None and avg_tokens is not None:
        cost_avg = round(avg_tokens * price / 1e6, 6)
    else:
        cost_avg = None
    return {"source": "Foundry evaluation run",
            "latency_p95_s": round(lat["p95_ms"] / 1000, 2) if lat.get("p95_ms") is not None else None,
            "latency_p50_s": round(lat["p50_ms"] / 1000, 2) if lat.get("p50_ms") is not None else None,
            "latency_samples": lat.get("sample_count"),
            "tokens_avg": avg_tokens, "measured_tokens": bool(tokens) and len(tokens) == len(pack["dataset"]),
            "cost_avg_usd": cost_avg}


def budget_rows(pack, base, cand):
    b = pack.get("budget") or {}
    checks = [("p95 latency (s)", "latency_p95_s", b.get("max_latency_p95_ratio", 1.5)),
              ("tokens per answer", "tokens_avg", b.get("max_tokens_ratio", 1.5)),
              ("cost per answer (USD)", "cost_avg_usd", b.get("max_cost_ratio"))]
    rows, ok_all = [], True
    for label, key, ratio in checks:
        bv, cv = base.get(key), cand.get(key)
        if ratio is None:
            rows.append(f"| {label} | {bv if bv is not None else 'n/a'} | {cv if cv is not None else 'n/a'} | not checked |")
            continue
        if bv is None or cv is None:
            # A budget that is set but can't be measured fails: "not measured" must not read as "within budget".
            rows.append(f"| {label} | {bv if bv is not None else 'n/a'} | {cv if cv is not None else 'n/a'} | FAIL: not reported by Foundry |")
            ok_all = False
            continue
        if key == "tokens_avg" and not (base["measured_tokens"] and cand["measured_tokens"]):
            rows.append(f"| {label} | {bv} | {cv} | FAIL: token usage missing from some answers |")
            ok_all = False
            continue
        limit = bv * float(ratio)
        ok = cv <= limit
        ok_all = ok_all and ok
        rows.append(f"| {label} | {bv} | {cv} | {'ok' if ok else 'FAIL'} (≤ {limit:.4g}, {float(ratio)}× baseline) |")
    return rows, ok_all


def run(path, out):
    from azure.ai.projects import AIProjectClient
    from azure.identity import AzureCliCredential

    pack = load_pack(path)
    state = ((governance(pack["candidate"]).get("lifecycle") or {}).get("state"))
    if state not in EVALUABLE:
        sys.exit(f"{pack['candidate']} is '{state}': only an onboarded model is evaluated for validation.")
    governance(pack["baseline"])   # the baseline must be onboarded too
    endpoint, connection = os.environ.get("EVAL_PROJECT_ENDPOINT"), os.environ.get("EVAL_CONNECTION")
    if not endpoint or not connection:
        sys.exit("GitHub variables EVAL_PROJECT_ENDPOINT and EVAL_CONNECTION (environment model-dev) name the platform "
                 "evaluation project and its gateway connection.")
    oai = AIProjectClient(endpoint=endpoint, credential=AzureCliCredential(process_timeout=60)).get_openai_client()

    definition = evaluation(oai, pack, connection)
    # One run at a time: both share the judge's token limit, and a throttled judge call leaves an item unscored.
    done = {}
    for role, model in (("baseline", pack["baseline"]), ("candidate", pack["candidate"])):
        done[role] = wait(oai, definition, run_model(oai, definition, pack, connection, model, role))
    scores = {role: rates(r) for role, r in done.items()}
    reports = {role: getattr(r, "report_url", None) for role, r in done.items()}

    margin = float(pack.get("margin", 0.0))
    expected = len(pack["dataset"])
    need = -(-expected * float(pack.get("min_coverage", 1.0)) // 1)
    rows, verdict, incomplete = [], True, set()
    for e, cfg in pack["evaluators"].items():
        b, c = scores["baseline"].get(e), scores["candidate"].get(e)
        if b is None or c is None:
            rows.append(f"| {e} | {b and b['rate']} | {c and c['rate']} | no result | | | FAIL |")
            verdict = False
            incomplete.update(role for role, x in (("baseline", b), ("candidate", c)) if x is None)
            continue
        if b["total"] < need or c["total"] < need:
            rows.append(f"| {e} | {b['passed']}/{b['total']} | {c['passed']}/{c['total']} | | | | FAIL: incomplete, "
                        f"at least {int(need)} of {expected} must be scored |")
            verdict = False
            incomplete.update(role for role, x in (("baseline", b), ("candidate", c)) if x["total"] < need)
            continue
        floor_ok = c["rate"] >= float(cfg.get("floor", 0.0))
        regression_ok = c["rate"] >= b["rate"] - margin
        hard_ok = e not in (pack.get("hard_stops") or []) or c["passed"] == c["total"]
        ok = floor_ok and regression_ok and hard_ok
        verdict = verdict and ok
        rows.append(f"| {e} | {b['passed']}/{b['total']} ({b['rate']:.0%}) | {c['passed']}/{c['total']} ({c['rate']:.0%}) "
                    f"| {'ok' if floor_ok else 'FAIL'} (≥ {float(cfg.get('floor', 0)):.0%}) "
                    f"| {'ok' if regression_ok else 'FAIL'} (≥ {b['rate'] - margin:.0%}) "
                    f"| {'ok' if hard_ok else 'FAIL'} | {'pass' if ok else 'FAIL'} |")
    for role in ("baseline", "candidate"):
        if done[role].status != "completed":
            verdict = False
            incomplete.add(role)
            rows.append(f"| ({role} run) | status {done[role].status} | | | | | FAIL |")
    why = {role: errors(oai, definition, done[role]) for role in sorted(incomplete)}

    perf = {role: foundry_budget(oai, definition, done[role], pack, model)
            for role, model in (("baseline", pack["baseline"]), ("candidate", pack["candidate"]))}
    brows, budget_ok = budget_rows(pack, perf["baseline"], perf["candidate"])
    verdict = verdict and budget_ok

    record = {"schema": "model-evaluation-record/1", "candidate": pack["candidate"], "baseline": pack["baseline"],
              "pack": pack["path"], "pack_version": pack["pack_version"], "dataset_sha": pack["dataset_sha"],
              "queries": expected, "judge": pack["judge"], "margin": margin, "min_coverage": pack.get("min_coverage", 1.0),
              "verdict": "pass" if verdict else "fail", "scores": scores, "budget": perf, "budget_ok": budget_ok,
              "unscored_reasons": why, "foundry": {"eval_id": definition.id, "reports": reports,
                                                    "runs": {role: r.id for role, r in done.items()}},
              "requested_by": os.environ.get("GITHUB_ACTOR"), "commit": os.environ.get("GITHUB_SHA", "local"),
              "run": os.environ.get("RUN_URL"),
              "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)

    links = " · ".join(f"[{role} report]({u})" for role, u in reports.items() if u)
    table = ("| Evaluator | Baseline | Candidate | Floor | No regression | Hard stop | Result |\n"
             "|---|---|---|---|---|---|---|\n" + "\n".join(rows))
    budget = "| Measure | Baseline | Candidate | Check |\n|---|---|---|---|\n" + "\n".join(brows)
    record["tables"] = {"scores": table, "budget": budget}
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
    summary(f"### Platform evaluation: `{pack['candidate']}` (candidate) vs `{pack['baseline']}` (baseline)\n\n"
            f"Pack v{pack['pack_version']} ({expected} queries, dataset `{pack['dataset_sha']}`) · judge "
            f"`{pack['judge']}` · margin {margin:.0%} · {links}\n\n{table}"
            + "".join(f"\n\n**Why {role} items weren't scored:**\n" + ("\n".join(f"- {m}" for m in msgs) or "- no error message; open the report")
                      for role, msgs in why.items())
            + f"\n\n**Budget** (source: Foundry evaluation run)\n\n{budget}\n\n**Verdict: {'PASS' if verdict else 'FAIL'}**"
            + ("" if verdict else ": the candidate stays onboarded; no validation is proposed."))
    if not verdict:
        sys.exit(f"Platform evaluation failed: {pack['candidate']} vs {pack['baseline']}")


def propose(path, record_file, blob):
    pack = load_pack(path)
    with open(record_file, encoding="utf-8") as fh:
        rec = json.load(fh)
    if rec.get("verdict") != "pass":
        sys.exit("Only a passed evaluation is proposed for validation.")
    gfile = os.path.join(model_folder(pack["candidate"]), "governance.yaml")
    with open(gfile, encoding="utf-8") as fh:
        text = fh.read()
    header = ""
    for line in text.splitlines(keepends=True):   # keep the file's leading comment
        if not line.startswith("#"):
            break
        header += line
    gov = yaml.safe_load(text) or {}
    old = (gov.get("lifecycle") or {}).get("state")
    gov["lifecycle"] = {
        "state": "validated",
        "replaces": pack["baseline"],
        "evaluation": {"verdict": "pass", "pack": f"{pack['path']} v{pack['pack_version']}",
                       "dataset_sha": pack["dataset_sha"], "queries": rec["queries"], "judge": pack["judge"],
                       "run": rec.get("run"), "record": blob, "at": rec["at"]},
    }
    with open(gfile, "w", encoding="utf-8") as fh:
        fh.write(header + yaml.safe_dump(gov, sort_keys=False))
    gov_doc = governance(pack["candidate"])
    reports = " · ".join(f"[{r} report in Foundry]({u})" for r, u in (rec["foundry"].get("reports") or {}).items() if u)
    print(f"""## What you are validating

The platform evaluation of **{pack['candidate']}** passed against **{pack['baseline']}**, the model it would replace.
Approving this pull request validates **{pack['candidate']}** for use: its lifecycle moves from **{old}** to
**validated**, and every project that uses {pack['baseline']} is told it can evaluate {pack['candidate']} for its own
agents. Nothing switches automatically: each project evaluates and approves its own agents.

| | |
|---|---|
| Risk tier | {gov_doc.get('tier')} (AICOE record {gov_doc.get('tier_record')}) |
| AI Impact Assessment | {gov_doc.get('aiia')} |
| Affects people | {'**Yes**' if gov_doc.get('affects_people') else 'No'} |
| Evaluation | {rec['queries']} queries, pack `{pack['path']}` v{pack['pack_version']}, judge `{pack['judge']}`, margin {rec['margin']:.0%} |
| Run | {rec.get('run')} · {reports} |
| Evidence (immutable) | `{blob}` in the records store |
| Started by | @{rec.get('requested_by')} (can't approve this: check separation-of-duties) |

**Scores** (pass rates; every evaluator must meet its floor and not regress beyond the margin)

{rec['tables']['scores']}

**Budget** (from the Foundry evaluation run)

{rec['tables']['budget']}

**Read this before approving:** {rec['queries']} queries prove the pipeline works, not the model's quality. With
15 queries one answer moves a pass rate by about 7 points. A bank's validation pack needs a few hundred queries per
use-case family, agreed with model risk.

## How to decide

- **Validate:** *Files changed* → *Review changes* → **Approve**.
- **Validate with conditions:** add them to `lifecycle.conditions` in `governance.yaml` as a suggestion in your
  review, then approve once they're in.
- **Send back or reject:** **Request changes** with your findings, or close this pull request with the reason.
""")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("run", "propose"):
        sys.exit(__doc__)
    if sys.argv[1] == "run":
        run(sys.argv[2], sys.argv[3])
    else:
        propose(sys.argv[2], sys.argv[3], sys.argv[4])
