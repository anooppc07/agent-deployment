"""Model adoption (test bed G5): this repository finds out from the gateway registry which models the platform has
validated, and opens an issue for each project that uses the model being replaced.

  model_adoption.py [days]     read the registry (APIM named values model-meta-*); for each model whose lifecycle is
                               validated and that replaces another model, open "Evaluate model <candidate> for project
                               <project>" in every project that allows the replaced model and not yet the candidate.
                               An issue that exists (open or closed) isn't opened again.

The platform (ai-gateway) validates a model in a reviewed pull request: model risk's approval sets governance.yaml
lifecycle.state validated and replaces: <baseline>, and the deployment copies both into the registry. Nothing is
written into this repository from outside: it reads the registry, as it reads models for onboarding.

Needs GITHUB_TOKEN (issues: write), GITHUB_REPOSITORY, and the gateway coordinates (APIM_*), read with the pipeline's
OIDC sign-in.
"""
import datetime
import json
import os
import sys

import requests
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent_pipeline import PROJECTS, arm, summary  # noqa: E402


def registry():
    r = arm("GET", "namedValues", params={"$filter": "startswith(name,'model-meta-')", "$top": "100"}).json()
    if r.get("nextLink"):
        sys.exit("More than 100 registry entries: page through them (not needed in the test bed).")
    return {e["name"]: e for e in (json.loads(nv["properties"]["value"]) for nv in r.get("value", [])
                                   if nv["properties"].get("value"))}


def projects():
    for name in sorted(os.listdir(PROJECTS)):
        f = os.path.join(PROJECTS, name, "project.yaml")
        if not os.path.exists(f):
            continue
        with open(f, encoding="utf-8") as fh:
            p = yaml.safe_load(fh) or {}
        if p.get("platform_evaluation"):
            continue
        yield name, p


def main(days="14"):
    repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"]
    api = f"https://api.github.com/repos/{repo}/issues"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    existing, page = set(), 1
    while True:
        batch = requests.get(api, headers=headers, params={"state": "all", "per_page": 100, "page": page}, timeout=30)
        if batch.status_code == 410:
            sys.exit("Issues are turned off in this repository (Settings > General > Features > Issues).")
        batch.raise_for_status()
        items = batch.json()
        existing |= {i["title"] for i in items if "pull_request" not in i}
        if len(items) < 100:
            break
        page += 1
    due = (datetime.date.today() + datetime.timedelta(days=int(days))).isoformat()

    rows = []
    reg = registry()
    for cand, entry in sorted(reg.items()):
        life = (entry.get("governance") or {}).get("lifecycle") or {}
        baseline = life.get("replaces")
        if entry.get("lifecycle") != "validated" or not baseline:
            continue
        ev = life.get("evaluation") or {}
        for project, p in projects():
            allowed = {m.get("name") for m in (p.get("models") or {}).get("allowed", [])}
            if baseline not in allowed:
                continue
            title = f"Evaluate model {cand} for project {project}"
            if cand in allowed:
                rows.append(f"| {cand} | {project} | already allowed (adoption under way or done) |")
                continue
            if title in existing:
                rows.append(f"| {cand} | {project} | issue exists |")
                continue
            body = (f"The platform has validated **`{cand}`** as the replacement for **`{baseline}`**, which "
                    f"`{project}` uses today.\n\n| | |\n|---|---|\n| Evaluate by | **{due}** |\n"
                    f"| Platform evaluation | pack {ev.get('pack', '?')}, {ev.get('queries', '?')} queries, judge "
                    f"`{ev.get('judge', '?')}`, verdict {ev.get('verdict', '?')} · [run]({ev.get('run', '')}) |\n"
                    f"| Evidence (immutable) | `{ev.get('record', '?')}` |\n"
                    f"| Validated in | the gateway registry (`model-meta-{cand.replace('.', '-')}`): lifecycle "
                    f"validated, approved by model risk |\n| Owner | {p.get('owner', '')} |\n\n"
                    "**What to do**\n"
                    f"1. Request evaluation-only access: add `{cand}` to `models.allowed` in "
                    f"`projects/{project}/project.yaml` with status `evaluating` (small token cap).\n"
                    f"2. Create new versions of your agents on `{cand}`, one or two per pull request.\n"
                    "3. Each version is evaluated against the agent's current version; only agents that pass are "
                    "pinned after approval. Agents that fail stay on the current model.\n\n"
                    f"`{baseline}` keeps serving until every agent has moved or the due date.")
            r = requests.post(api, headers=headers, json={"title": title, "body": body}, timeout=30)
            r.raise_for_status()
            rows.append(f"| {cand} | {project} | opened [#{r.json()['number']}]({r.json()['html_url']}) |")
    summary("### Model adoption (from the gateway registry)\n\n| Validated model | Project | Issue |\n|---|---|---|\n"
            + ("\n".join(rows) or "| (no validated model replaces one that a project uses) | | |"))


if __name__ == "__main__":
    main(*sys.argv[1:])
