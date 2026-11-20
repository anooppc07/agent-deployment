"""Model-level evaluation (test bed G4) and project notification (G5).

  model_eval.py run <pack.yaml>                 evaluate the pack's candidate against its baseline; fails on a failed verdict
  model_eval.py notify <pack.yaml> [days]       open an issue per project that uses the baseline (after approval)

run: in the platform evaluation project (platform-eval), one probe agent per model (neutral instructions, no tools)
on <connection>/<model>, so calls go through the AI gateway like any project's. Both probes answer the same queries;
the same judge scores both. The verdict is four checks, all required, after a coverage check (every query scored for both models):
  floors         candidate pass rate per evaluator >= its floor
  hard stops     listed evaluators pass every item
  no regression  candidate pass rate >= baseline pass rate - margin (non-inferiority), per evaluator
  budget         each probe answers the pack's queries once more, through the gateway: p95 latency, average tokens
                 per answer and, when the pack lists prices, average cost per answer, each within the pack's ratio
                 of the baseline's (budget: max_latency_p95_ratio, max_tokens_ratio, max_cost_ratio, prices)
The judge is a setting: the pack's judge_model, else the GitHub variable EVAL_JUDGE_MODEL (a gateway model name).
Each run records the pack version, dataset hash, judge, models and commit, in Foundry (run metadata) and in
.pipeline/model-eval.json (uploaded as a workflow artifact).

notify: needs GITHUB_TOKEN (issues: write) and GITHUB_REPOSITORY. One open issue per project and candidate; an
existing open issue with the same title is left as it is.
"""
import datetime
import hashlib
import json
import os
import sys
import time

import requests
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent_pipeline import ITEM_SCHEMA, PROJECTS, STATE, cred, load_project, summary  # noqa: E402

EVAL_PROJECT = "platform-eval"
PROBE_INSTRUCTIONS = "You are a helpful assistant. Answer the user's request accurately and concisely."
EVALUATORS = {"relevance", "coherence", "fluency", "intent_resolution", "task_adherence"}


def load_pack(path):
    with open(path, encoding="utf-8") as fh:
        pack = yaml.safe_load(fh)
    for key in ("candidate", "baseline", "pack_version", "evaluators", "dataset"):
        if key not in pack:
            sys.exit(f"{path}: '{key}' is required")
    unknown = set(pack["evaluators"]) - EVALUATORS
    if unknown:
        sys.exit(f"{path}: unknown evaluators {sorted(unknown)}; use {sorted(EVALUATORS)}")
    if not set(pack.get("hard_stops") or []) <= set(pack["evaluators"]):
        sys.exit(f"{path}: hard_stops must be listed under evaluators")
    pack["judge"] = pack.get("judge_model") or os.environ.get("EVAL_JUDGE_MODEL") or sys.exit(
        "No judge: set judge_model in the pack or the GitHub variable EVAL_JUDGE_MODEL (a gateway model name)")
    pack["dataset_sha"] = hashlib.sha256(json.dumps(pack["dataset"], sort_keys=True).encode()).hexdigest()[:12]
    return pack


def probe_name(model):
    return "probe-" + model.replace(".", "-")


def criteria(pack, connection):
    out = []
    for e in pack["evaluators"]:
        if e in ("intent_resolution", "task_adherence"):
            mapping = {"query": "{{item.instructed_query}}", "response": "{{sample.output_items}}"}
        else:
            mapping = {"query": "{{item.query}}", "response": "{{sample.output_text}}"}
        out.append({"type": "azure_ai_evaluator", "name": e, "evaluator_name": f"builtin.{e}",
                    "initialization_parameters": {"deployment_name": f"{connection}/{pack['judge']}"},
                    "data_mapping": mapping})
    return out


def evaluation(oai, pack, connection):
    """One Foundry evaluation per candidate, pack version and judge, so baseline and candidate runs sit side by side."""
    tc = criteria(pack, connection)
    key = hashlib.sha256(json.dumps([tc, ITEM_SCHEMA], sort_keys=True).encode()).hexdigest()[:12]
    for e in oai.evals.list(limit=100):
        if (e.metadata or {}).get("model_eval_key") == key:
            return e
    return oai.evals.create(
        name=f"model evaluation: {pack['candidate']} vs {pack['baseline']} (pack v{pack['pack_version']})",
        metadata={"model_eval_key": key, "candidate": pack["candidate"], "baseline": pack["baseline"],
                  "pack_version": str(pack["pack_version"]), "judge": pack["judge"], "pipeline": "model-evaluation"},
        data_source_config={"type": "custom", "include_sample_schema": True, "item_schema": ITEM_SCHEMA},
        testing_criteria=tc)


def run_model(oai, definition, pack, model, version, role):
    run = oai.evals.runs.create(
        eval_id=definition.id, name=f"{role} {model}",
        metadata={"model": model, "role": role, "probe_version": str(version), "pack_version": str(pack["pack_version"]),
                  "dataset_sha": pack["dataset_sha"], "judge": pack["judge"],
                  "git_commit": os.environ.get("GITHUB_SHA", "local")[:40], "pipeline": "model-evaluation"},
        data_source={"type": "azure_ai_target_completions",
                     "source": {"type": "file_content", "content": [{"item": {
                         "query": d["query"],
                         "instructed_query": [
                             {"role": "system", "content": [{"type": "text", "text": PROBE_INSTRUCTIONS}]},
                             {"role": "user", "content": [{"type": "text", "text": d["query"]}]}]}}
                         for d in pack["dataset"]]},
                     "input_messages": {"type": "template", "template": [{
                         "type": "message", "role": "user", "content": {"type": "input_text", "text": "{{item.query}}"}}]},
                     "target": {"type": "azure_ai_agent", "name": probe_name(model), "version": str(version)}})
    print(f"{role} {model}: evaluation running ({len(pack['dataset'])} queries)")
    return run


def wait(oai, definition, run):
    end = time.time() + 2400
    while time.time() < end:
        r = oai.evals.runs.retrieve(run_id=run.id, eval_id=definition.id)
        if r.status in ("completed", "failed", "canceled"):
            return r
        time.sleep(15)
    sys.exit(f"evaluation run {run.id} still running after 40 minutes")


def errors(oai, definition, r, limit=3):
    """Why items weren't scored: the first few output items' errors (for example the gateway's 429)."""
    out = []
    try:
        for item in oai.evals.runs.output_items.list(run_id=r.id, eval_id=definition.id, limit=100):
            sample = getattr(item, "sample", None)
            err = getattr(sample, "error", None) if sample is not None else None
            msg = (getattr(err, "message", None) or getattr(err, "code", None)) if err else None
            if not msg:
                # An evaluator that errored on this item: its result has no pass or fail. Show it raw, whatever its shape.
                for res in (getattr(item, "results", None) or []):
                    d = res if isinstance(res, dict) else (res.model_dump() if hasattr(res, "model_dump") else vars(res))
                    if d.get("passed") is None:
                        err = ((d.get("sample") or {}).get("error") or {})
                        text = err.get("message") or json.dumps({k: v for k, v in d.items() if k != "name"}, default=str)
                        print(f"  unscored item, {d.get('name', '?')}: {text[:1000]}")   # whole message in the job log
                        msg = f"{d.get('name', '?')}: {text[:600]}"
                        break
            if msg:
                out.append(str(msg)[:160])
            if len(out) >= limit:
                break
    except Exception as e:  # the listing is diagnostics only
        out.append(f"(could not list output items: {e})")
    return out


def rates(r):
    out = {}
    for pc in (getattr(r, "per_testing_criteria_results", None) or []):
        total = pc.passed + pc.failed
        out[pc.testing_criteria] = {"passed": pc.passed, "total": total, "rate": round(pc.passed / total, 3) if total else 0.0}
    return out


def measure(oai, model, version, pack):
    """Latency (seconds, end to end through the gateway) and tokens per answer for one probe version."""
    latencies, tokens = [], []
    for d in pack["dataset"]:
        start = time.time()
        resp = oai.responses.create(input=d["query"], extra_body={"agent_reference": {
            "name": probe_name(model), "version": str(version), "type": "agent_reference"}})
        latencies.append(time.time() - start)
        u = getattr(resp, "usage", None)
        tokens.append((getattr(u, "input_tokens", 0) or 0) + (getattr(u, "output_tokens", 0) or 0) if u else 0)
    lat = sorted(latencies)
    p95 = lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))]
    price = ((pack.get("budget") or {}).get("prices") or {}).get(model)   # USD per million tokens (blended)
    avg_tokens = sum(tokens) / len(tokens)
    return {"latency_p95_s": round(p95, 2), "latency_avg_s": round(sum(lat) / len(lat), 2),
            "tokens_avg": round(avg_tokens, 1), "measured_tokens": all(tokens),
            "cost_avg_usd": round(avg_tokens * price / 1e6, 6) if price is not None else None}


def budget_rows(pack, base, cand):
    """Rows and verdict for the budget check: candidate within the pack's ratio of the baseline."""
    b = pack.get("budget") or {}
    checks = [("p95 latency (s)", "latency_p95_s", b.get("max_latency_p95_ratio", 1.5)),
              ("tokens per answer", "tokens_avg", b.get("max_tokens_ratio", 1.5)),
              ("cost per answer (USD)", "cost_avg_usd", b.get("max_cost_ratio"))]
    rows, ok_all = [], True
    for label, key, ratio in checks:
        bv, cv = base.get(key), cand.get(key)
        if ratio is None or bv is None or cv is None:
            rows.append(f"| {label} | {bv if bv is not None else 'n/a'} | {cv if cv is not None else 'n/a'} | not checked |")
            continue
        if key == "tokens_avg" and not (base["measured_tokens"] and cand["measured_tokens"]):
            rows.append(f"| {label} | {bv} | {cv} | FAIL: token usage missing from some responses |")
            ok_all = False
            continue
        limit = bv * float(ratio)
        ok = cv <= limit
        ok_all = ok_all and ok
        rows.append(f"| {label} | {bv} | {cv} | {'ok' if ok else 'FAIL'} (≤ {limit:.4g}, {float(ratio)}× baseline) |")
    return rows, ok_all


def run(path):
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import PromptAgentDefinition

    pack = load_pack(path)
    p = load_project(EVAL_PROJECT)
    connection = p["models"]["connection"]
    allowed = {m["name"] for m in p["models"]["allowed"]}
    missing = {pack["candidate"], pack["baseline"], pack["judge"]} - allowed
    if missing:
        sys.exit(f"projects/{EVAL_PROJECT}/project.yaml must allow {sorted(missing)} (models.allowed)")
    client = AIProjectClient(endpoint=p["foundry_project_endpoint"], credential=cred)
    oai = client.get_openai_client()

    versions = {}
    for model in (pack["baseline"], pack["candidate"]):
        v = client.agents.create_version(agent_name=probe_name(model), definition=PromptAgentDefinition(
            model=f"{connection}/{model}", instructions=PROBE_INSTRUCTIONS))
        versions[model] = v.version
        print(f"probe {probe_name(model)} v{v.version} on {connection}/{model}")

    definition = evaluation(oai, pack, connection)
    # One run at a time: both runs share the judge's token limit, and a throttled judge call leaves an item unscored.
    done = {}
    for role, model in (("baseline", pack["baseline"]), ("candidate", pack["candidate"])):
        done[role] = wait(oai, definition, run_model(oai, definition, pack, model, versions[model], role))
    scores = {role: rates(r) for role, r in done.items()}
    reports = {role: getattr(r, "report_url", None) for role, r in done.items()}

    margin = float(pack.get("margin", 0.0))
    rows, verdict, incomplete = [], True, set()
    for e, cfg in pack["evaluators"].items():
        b = scores["baseline"].get(e)
        c = scores["candidate"].get(e)
        if b is None or c is None:
            rows.append(f"| {e} | {b and b['rate']} | {c and c['rate']} | no result | | | FAIL |")
            verdict = False
            continue
        # Coverage: every query scored for both models. An item with an evaluator error is neither passed nor
        # failed, so without this check the pass rate would be over fewer items and could hide a gap.
        expected = len(pack["dataset"])
        if b["total"] < expected or c["total"] < expected:
            rows.append(f"| {e} | {b['passed']}/{b['total']} | {c['passed']}/{c['total']} | | | | "
                        f"FAIL: incomplete, {expected - b['total']} baseline and {expected - c['total']} candidate "
                        f"of {expected} not scored |")
            verdict = False
            incomplete.update(role for role, x in (("baseline", b), ("candidate", c)) if x["total"] < expected)
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
            rows.append(f"| ({role} run) | status {done[role].status} | | | | | FAIL |")

    why = {role: errors(oai, definition, done[role]) for role in sorted(incomplete)}

    perf = {role: measure(oai, model, versions[model], pack)
            for role, model in (("baseline", pack["baseline"]), ("candidate", pack["candidate"]))}
    brows, budget_ok = budget_rows(pack, perf["baseline"], perf["candidate"])
    verdict = verdict and budget_ok

    record = {"candidate": pack["candidate"], "baseline": pack["baseline"], "pack": path,
              "pack_version": pack["pack_version"], "dataset_sha": pack["dataset_sha"], "judge": pack["judge"],
              "margin": margin, "verdict": "pass" if verdict else "fail", "scores": scores, "reports": reports,
              "probe_versions": versions, "eval_id": definition.id, "budget": perf, "budget_ok": budget_ok, "unscored_reasons": why,
              "git_commit": os.environ.get("GITHUB_SHA", "local"),
              "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, "model-eval.json"), "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)

    links = " · ".join(f"[{role} report]({u})" for role, u in reports.items() if u)
    summary(f"### Model evaluation: `{pack['candidate']}` (candidate) vs `{pack['baseline']}` (baseline)\n\n"
            f"Pack v{pack['pack_version']} ({len(pack['dataset'])} queries, dataset `{pack['dataset_sha']}`) · "
            f"judge `{pack['judge']}` · margin {margin:.0%} · {links}\n\n"
            "| Evaluator | Baseline | Candidate | Floor | No regression | Hard stop | Result |\n"
            "|---|---|---|---|---|---|---|\n" + "\n".join(rows)
            + "".join(f"\n\n**Why {role} items weren't scored:**\n" + ("\n".join(f"- {m}" for m in msgs) or
                      "no error message in the output items; open the report") for role, msgs in why.items())
            + "\n\n**Budget** (each query answered once more, end to end through the gateway)\n\n"
            "| Measure | Baseline | Candidate | Check |\n|---|---|---|---|\n" + "\n".join(brows) +
            f"\n\n**Verdict: {'PASS' if verdict else 'FAIL'}**"
            + ("" if verdict else ": the candidate stays in INTAKE; no project is notified."))
    if not verdict:
        sys.exit(f"Model evaluation failed: {pack['candidate']} vs {pack['baseline']}")


def consumers(baseline):
    out = []
    for name in sorted(os.listdir(PROJECTS)):
        f = os.path.join(PROJECTS, name, "project.yaml")
        if name == EVAL_PROJECT or not os.path.exists(f):
            continue
        with open(f, encoding="utf-8") as fh:
            p = yaml.safe_load(fh)
        if any(m.get("name") == baseline for m in (p.get("models") or {}).get("allowed", [])):
            out.append((name, p.get("owner", "")))
    return out


def notify(path, days="14"):
    pack = load_pack(path)
    repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"]
    api = f"https://api.github.com/repos/{repo}/issues"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    due = (datetime.date.today() + datetime.timedelta(days=int(days))).isoformat()
    record = {}
    if os.path.exists(os.path.join(STATE, "model-eval.json")):
        with open(os.path.join(STATE, "model-eval.json"), encoding="utf-8") as fh:
            record = json.load(fh)
    open_titles = {i["title"] for i in requests.get(api, headers=headers, params={"state": "open", "per_page": 100},
                                                     timeout=30).json() if "pull_request" not in i}
    run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    rows = []
    for project, owner in consumers(pack["baseline"]):
        title = f"Evaluate model {pack['candidate']} for project {project}"
        if title in open_titles:
            rows.append(f"| {project} | already open |")
            continue
        body = (f"The platform has approved **`{pack['candidate']}`** as the replacement candidate for "
                f"**`{pack['baseline']}`**, which `{project}` uses today.\n\n"
                f"| | |\n|---|---|\n| Evaluate by | **{due}** |\n| Model-level result | [run]({run_url}) · pack "
                f"v{pack['pack_version']} · judge `{pack['judge']}` · verdict {record.get('verdict', 'pass')} |\n"
                f"| Owner | {owner} |\n\n"
                "**What to do**\n"
                f"1. Request evaluation-only access: add `{pack['candidate']}` to `models.allowed` in "
                f"`projects/{project}/project.yaml` with status `evaluating` (small token cap).\n"
                f"2. Create new versions of your agents on `{pack['candidate']}`, one or two per pull request.\n"
                "3. Each version is evaluated against the agent's current version; only agents that pass are pinned "
                "after approval. Agents that fail stay on the current model.\n\n"
                f"`{pack['baseline']}` keeps serving until every agent has moved or the due date.")
        r = requests.post(api, headers=headers, json={"title": title, "body": body}, timeout=30)
        r.raise_for_status()
        rows.append(f"| {project} | [#{r.json()['number']}]({r.json()['html_url']}) |")
    summary(f"### Projects notified: `{pack['candidate']}` (due {due})\n\n| Project | Issue |\n|---|---|\n"
            + ("\n".join(rows) or f"| (none: no project uses `{pack['baseline']}`) | |"))


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in ("run", "notify"):
        sys.exit(__doc__)
    {"run": run, "notify": notify}[sys.argv[1]](*sys.argv[2:])
