"""Model adoption (test bed G5): this repository finds out from the gateway registry which models the platform has
validated, and opens an issue for each project that uses the model being replaced.

  model_adoption.py [days]     read the registry (APIM named values model-meta-*); for each model whose lifecycle is
                               validated and that replaces another model, for every project that allows the replaced
                               model and not yet the candidate:
                                 - open "Evaluate model <candidate> for project <project>" (once: an issue that exists,
                                   open or closed, isn't opened again);
                                 - open a draft pull request (branch adopt/<candidate>-<project>, once) that proposes
                                   the project's adoption: the candidate in models.allowed with status evaluating (the
                                   cap), and each agent on the replaced model moved to the candidate. The project team
                                   decides: they edit it (instructions, which agents), mark it ready for review (that
                                   starts its checks), and their code owners approve it, or they close it. Merging
                                   creates the new agent versions; each is evaluated against its live version and
                                   pinned only if it passes and is approved (agent-deploy).

The platform (ai-gateway) validates a model in a reviewed pull request: model risk's approval sets governance.yaml
lifecycle.state validated and replaces: <baseline>, and the deployment copies both into the registry. Nothing is
written into this repository from outside: it reads the registry, as it reads models for onboarding.

Needs GITHUB_TOKEN (issues: write), GITHUB_REPOSITORY, and the gateway coordinates (APIM_*), read with the pipeline's
OIDC sign-in.
"""
import datetime
import glob
import json
import os
import re
import subprocess
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


EVALUATING_TPM, EVALUATING_TPD = 5000, 100000   # the evaluating cap (agent_pipeline.EVALUATING_TPM_CAP)


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def remote_branch(branch):
    return bool(subprocess.run(["git", "ls-remote", "--exit-code", "--heads", "origin", branch],
                               capture_output=True).returncode == 0)


def propose(project, p, cand, baseline, issue_no, ev):
    """The project's adoption, as a draft pull request the project team decides on. Returns (url, agents moved)."""
    branch = f"adopt/{cand}-{project}"
    if remote_branch(branch):
        return None, None
    root = os.path.join(PROJECTS, project)
    git("checkout", "-q", "-B", branch, "origin/main")
    # 1. Evaluation-only access (P1): the candidate after the replaced model in models.allowed, status evaluating.
    pf = os.path.join(root, "project.yaml")
    with open(pf, encoding="utf-8") as fh:
        lines = fh.readlines()
    start = next(i for i, l in enumerate(lines) if re.match(rf"^\s*- name: {re.escape(baseline)}\b", l))
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = start + 1
    while end < len(lines) and lines[end].strip() and len(lines[end]) - len(lines[end].lstrip()) > indent:
        end += 1
    pad = " " * indent
    block = [f"{pad}- name: {cand:<29}# issue #{issue_no}: validated by the platform as {baseline}'s replacement\n",
             f"{pad}  status: evaluating                   # evaluation only (P1): no agent is served on it until it passes and is approved\n",
             f"{pad}  tokens_per_minute: {EVALUATING_TPM:<18}# the evaluating cap\n",
             f"{pad}  tokens_per_day: {EVALUATING_TPD}\n"]
    lines[end:end] = block
    with open(pf, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    # 2. Each agent on the replaced model gets a new version on the candidate (P2), evaluated against its live one.
    moved = []
    for af in sorted(glob.glob(os.path.join(root, "agents", "*", "agent.yaml"))):
        with open(af, encoding="utf-8") as fh:
            text = fh.read()
        new, n = re.subn(rf"^model:\s*(\S+)/{re.escape(baseline)}\b.*$",
                         lambda m: f"model: {m.group(1)}/{cand:<16}# issue #{issue_no}: evaluated against the live version on {baseline}; pinned only if it passes and is approved",
                         text, flags=re.M)
        if n:
            with open(af, "w", encoding="utf-8") as fh:
                fh.write(new)
            moved.append(os.path.basename(os.path.dirname(af)))
    git("add", "-A", root)
    git("-c", "user.name=model-adoption", "-c", "user.email=model-adoption@users.noreply.github.com",
        "commit", "-q", "-m", f"{project}: propose adopting {cand} (issue #{issue_no})",
        "-m", f"Evaluation-only access to {cand}; agents moved from {baseline}: {', '.join(moved) or 'none'}.")
    git("push", "-q", "origin", branch)
    body = (f"**Proposal for the `{project}` team** (opened by `model-adoption`; see #{issue_no}). The platform has "
            f"validated **`{cand}`** as the replacement for **`{baseline}`**. This draft is what adopting it would "
            "change here. Nothing happens until your team decides.\n\n"
            "| | |\n|---|---|\n"
            f"| `project.yaml` | `{cand}` with status **evaluating** ({EVALUATING_TPM:,} tokens per minute): for the agents' evaluation only, not served |\n"
            f"| Agents moved to `{cand}` | {', '.join(f'`{a}`' for a in moved) or 'none'} |\n"
            f"| Platform evaluation | pack {ev.get('pack', '?')}, judge `{ev.get('judge', '?')}` · [run]({ev.get('run', '')}) · evidence `{ev.get('record', '?')}` |\n\n"
            "**How to decide**\n"
            f"1. Review it. Change what you need on this branch: an agent may need new instructions on `{cand}`; "
            "drop an agent you don't want to move (put its model back).\n"
            "2. Click **Ready for review**. That starts the checks (onboarding plan, validation) and asks your code "
            "owners to review.\n"
            "3. Approve and merge. The new agent versions are created and each is evaluated against its live version; "
            "an agent that passes is pinned only after the use-case owner approves it (environment agent-approval). "
            f"An agent that fails stays on `{baseline}`.\n\n"
            f"Not moving? Close this pull request with the reason; `{baseline}` keeps serving.\n\nRefs #{issue_no}")
    r = requests.post(f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/pulls", timeout=30,
                      headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"},
                      json={"title": f"{project}: adopt {cand} (evaluation first)", "head": branch, "base": "main",
                            "body": body, "draft": True})
    if r.status_code == 403:
        sys.exit("GitHub Actions may not open pull requests here: Settings > Actions > General > Workflow permissions > "
                 "Allow GitHub Actions to create and approve pull requests, then re-run.")
    r.raise_for_status()
    git("checkout", "-q", "main")
    return r.json()["html_url"], moved


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
    existing, page = {}, 1
    while True:
        batch = requests.get(api, headers=headers, params={"state": "all", "per_page": 100, "page": page}, timeout=30)
        if batch.status_code == 410:
            sys.exit("Issues are turned off in this repository (Settings > General > Features > Issues).")
        batch.raise_for_status()
        items = batch.json()
        existing.update({i["title"]: i["number"] for i in items if "pull_request" not in i})
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
                rows.append(f"| {cand} | {project} | already allowed (adoption under way or done) | |")
                continue
            if title in existing:
                no = existing[title]
                url, moved = propose(project, p, cand, baseline, no, ev)
                rows.append(f"| {cand} | {project} | #{no} | {f'draft [proposal]({url}): {len(moved)} agents' if url else 'proposal exists'} |")
                if url:
                    requests.post(f"{api}/{no}/comments", headers=headers, timeout=30,
                                  json={"body": f"Draft proposal for adopting `{cand}`: {url}. Review it, change what you need, then mark it ready for review."})
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
            no = r.json()["number"]
            url, moved = propose(project, p, cand, baseline, no, ev)
            if url:
                requests.post(f"{api}/{no}/comments", headers=headers, timeout=30,
                              json={"body": f"Draft proposal for adopting `{cand}`: {url}. Review it, change what you need, then mark it ready for review."})
            rows.append(f"| {cand} | {project} | opened [#{no}]({r.json()['html_url']}) | {f'draft [proposal]({url})' if url else 'proposal exists'} |")
    summary("### Model adoption (from the gateway registry)\n\n| Validated model | Project | Issue | Proposal |\n|---|---|---|---|\n"
            + ("\n".join(rows) or "| (no validated model replaces one that a project uses) | | | |"))


if __name__ == "__main__":
    main(*sys.argv[1:])
