"""Model adoption (test bed G5): this repository finds out from the gateway registry which models the platform has
validated, and opens an issue for each project that uses the model being replaced.

  model_adoption.py available <project>
                               after a release (agent-deploy publish): for each model the project still has as
                               evaluating and on which this release pinned agents, propose making it available with a
                               working share of the model's capacity (branch available/<model>-<project>, once), for
                               the AI platform to approve
  model_adoption.py held <project>
                               after a release: agents that failed on the replacement of a retiring model (and stay on
                               it) are posted on the project's retirement issue, with the date and the options
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


def existing_proposal(branch):
    """(url, kind) for a proposal branch that exists without a pull request (a person opens it from the link); (None,
    None) when it has a pull request; False when the branch doesn't exist."""
    repo = os.environ["GITHUB_REPOSITORY"]
    if not remote_branch(branch):
        return False
    prs = requests.get(f"https://api.github.com/repos/{repo}/pulls", timeout=30, params={
        "head": f"{repo.split('/')[0]}:{branch}", "state": "all"},
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"}).json()
    return (None, None) if prs else (f"https://github.com/{repo}/compare/main...{branch}?expand=1", "open")


def open_proposal(branch, paths, title, body, draft=True):
    """Commit the working-tree changes under paths on branch, push it, and open the pull request; where workflows may
    not open pull requests, return the link a person opens it from. The commit message carries the whole proposal:
    GitHub fills in the pull request from it. Returns (url, kind): kind "pr" or "open"."""
    repo = os.environ["GITHUB_REPOSITORY"]
    git("add", "-A", *paths)
    git("-c", "user.name=model-adoption", "-c", "user.email=model-adoption@users.noreply.github.com",
        "commit", "-q", "-m", title, "-m", body)
    git("push", "-q", "origin", branch)
    r = requests.post(f"https://api.github.com/repos/{repo}/pulls", timeout=30,
                      headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"},
                      json={"title": title, "head": branch, "base": "main", "body": body, "draft": draft})
    git("checkout", "-q", "main")
    if r.status_code == 403:
        # The organisation doesn't let workflows open pull requests (common in banks: the setting "Allow GitHub
        # Actions to create and approve pull requests" is off). Then a person opens it from the branch: one click,
        # and as its author is a person, its checks run at once.
        print("Workflows may not open pull requests here: the proposal is a branch with a link to open it.")
        return f"https://github.com/{repo}/compare/main...{branch}?expand=1", "open"
    r.raise_for_status()
    return r.json()["html_url"], "pr"


def propose(project, p, cand, baseline, issue_no, ev):
    """The project's adoption, as a draft pull request the project team decides on. Returns (url, agents moved)."""
    branch = f"adopt/{cand}-{project}"
    found = existing_proposal(branch)
    if found is not False:
        return found
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
    body = (f"**Proposal for the `{project}` team** (opened by `model-adoption`; see #{issue_no}). The platform has "
            f"validated **`{cand}`** as the replacement for **`{baseline}`**. This is what adopting it would "
            "change here. Nothing happens until your team decides.\n\n"
            "| | |\n|---|---|\n"
            f"| `project.yaml` | `{cand}` with status **evaluating** ({EVALUATING_TPM:,} tokens per minute): for the agents' evaluation only, not served |\n"
            f"| Agents moved to `{cand}` | {', '.join(f'`{a}`' for a in moved) or 'none'} |\n"
            f"| Platform evaluation | pack {ev.get('pack', '?')}, judge `{ev.get('judge', '?')}` · [run]({ev.get('run', '')}) · evidence `{ev.get('record', '?')}` |\n\n"
            "**How to decide**\n"
            f"1. Review it. Change what you need on this branch: an agent may need new instructions on `{cand}`; "
            "drop an agent you don't want to move (put its model back).\n"
            "2. If this is a draft, click **Ready for review**: that starts the checks (onboarding plan, validation) "
            "and asks your code owners to review.\n"
            "3. Approve and merge. The new agent versions are created and each is evaluated against its live version; "
            "an agent that passes is pinned only after the use-case owner approves it (environment agent-approval). "
            f"An agent that fails stays on `{baseline}`.\n\n"
            f"Not moving? Close this pull request with the reason; `{baseline}` keeps serving.\n\nRefs #{issue_no}")
    url, kind = open_proposal(branch, [root], f"{project}: adopt {cand} (evaluation first)", body)
    return url, ("open" if kind == "open" else moved)


def proposal_row(url, moved):
    if not url:
        return "proposal exists"
    return f"[open the pull request]({url}) (a person opens it)" if moved == "open" else f"draft [proposal]({url})"


def announce(api, headers, no, cand, url, moved, what=None):
    """One comment on the issue per proposal: the draft pull request, or the link a person opens it from."""
    if not url:
        return
    marker = f"<!-- adoption-proposal {cand} -->"
    comments = requests.get(f"{api}/{no}/comments", headers=headers, timeout=30, params={"per_page": 100}).json()
    if any(marker in (c.get("body") or "") for c in comments):
        return
    if moved == "open":
        text = (f"{marker}\nThe proposal for {what or f'adopting `{cand}`'} is ready on a branch: **[open the pull request]({url})**. "
                "Workflows may not open pull requests in this repository, so a person on the project team opens it; "
                "its checks start at once. Review it, change what you need, then ask your code owners to approve it.")
    else:
        text = (f"{marker}\nProposal for {what or f'adopting `{cand}`'}: {url}. Review it, change what you need, then mark it "
                "ready for review (that starts its checks).")
    requests.post(f"{api}/{no}/comments", headers=headers, timeout=30, json={"body": text})


REMINDERS = (30, 14, 7, 1)   # days before a retirement date


def agents_on(project, model):
    out = []
    for af in sorted(glob.glob(os.path.join(PROJECTS, project, "agents", "*", "agent.yaml"))):
        with open(af, encoding="utf-8") as fh:
            m = (yaml.safe_load(fh) or {}).get("model", "")
        if str(m).split("/")[-1] == model:
            out.append(os.path.basename(os.path.dirname(af)))
    return out


def propose_cleanup(project, model, repl, issue_no, moving):
    """The project already allows the replacement: move the agents still on the retiring model to it, or, when none
    is left, remove the retiring model from models.allowed. Returns (url, kind) or (None, None)."""
    branch = f"retire/{model}-{project}"
    found = existing_proposal(branch)
    if found is not False:
        return found
    root = os.path.join(PROJECTS, project)
    git("checkout", "-q", "-B", branch, "origin/main")
    if moving:
        for a in moving:
            af = os.path.join(root, "agents", a, "agent.yaml")
            with open(af, encoding="utf-8") as fh:
                text = fh.read()
            text = re.sub(rf"^model:\s*(\S+)/{re.escape(model)}\b.*$",
                          lambda m: f"model: {m.group(1)}/{repl:<16}# issue #{issue_no}: {model} retires; evaluated against the live version, pinned only if it passes and is approved",
                          text, flags=re.M)
            with open(af, "w", encoding="utf-8") as fh:
                fh.write(text)
        what = (f"Moves {', '.join(f'`{a}`' for a in moving)} from `{model}` to `{repl}`. Each new version is evaluated "
                "against its live version and pinned only if it passes and the use-case owner approves.")
        title = f"{project}: move agents off {model} (retiring) to {repl}"
    else:
        pf = os.path.join(root, "project.yaml")
        with open(pf, encoding="utf-8") as fh:
            lines = fh.readlines()
        start = next(i for i, l in enumerate(lines) if re.match(rf"^\s*- name: {re.escape(model)}\b", l))
        indent = len(lines[start]) - len(lines[start].lstrip())
        end = start + 1
        while end < len(lines) and lines[end].strip() and len(lines[end]) - len(lines[end].lstrip()) > indent:
            end += 1
        del lines[start:end]
        with open(pf, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        what = (f"No agent uses `{model}` any more, and `{repl}` is already allowed. Removes `{model}` from "
                "`models.allowed`, which also frees its capacity for other projects.")
        title = f"{project}: remove {model} (retiring)"
    body = (f"**For the `{project}` team** (opened by `model-adoption`; see #{issue_no}). {what}\n\n"
            f"Not ready? Close this pull request with the reason; the issue keeps reminding until the date.\n\nRefs #{issue_no}")
    return open_proposal(branch, [root], title, body, draft=False)


def retirements(reg, api, headers, existing, open_issues):
    """For each retiring model: an issue per project that still allows it, with the date and the replacement, and a
    proposal (adopt the replacement, move the agents, or remove the model); reminders before the date."""
    rows = []
    today = datetime.date.today()
    for model, entry in sorted(reg.items()):
        if entry.get("lifecycle") != "retiring":
            continue
        life = (entry.get("governance") or {}).get("lifecycle") or {}
        ret = life.get("retirement") or {}
        repl, date = ret.get("replacement"), str(ret.get("date"))
        left = (datetime.date.fromisoformat(date) - today).days
        rev = (((reg.get(repl) or {}).get("governance") or {}).get("lifecycle") or {}).get("evaluation") or {}
        for project, p in projects():
            allowed = {m.get("name") for m in (p.get("models") or {}).get("allowed", [])}
            if model not in allowed:
                continue
            on = agents_on(project, model)
            title = f"Model {model} retires on {date}: move to {repl} ({project})"
            no = existing.get(title)
            if no is None:
                body = (f"**`{model}` retires on {date}** ({left} days). On that date the gateway answers calls to it with "
                        f"410 Gone, for every project. Its replacement is **`{repl}`**, validated by model risk as the "
                        f"replacement of `{model}`.\n\n| | |\n|---|---|\n"
                        f"| Reason | {ret.get('reason', '')} |\n"
                        f"| Replacement's platform evaluation | pack {rev.get('pack', '?')}, against `{model}` {rev.get('baseline_version', '?')}, judge `{rev.get('judge', '?')}` · [run]({rev.get('run', '')}) · evidence `{rev.get('record', '?')}` |\n"
                        f"| Agents of `{project}` on `{model}` | {', '.join(f'`{a}`' for a in on) or 'none'} |\n"
                        f"| Owner | {p.get('owner', '')} |\n\n"
                        "**What to do:** review the proposal linked below and merge it. "
                        + ("Your agents get new versions on the replacement; each is evaluated against its live version "
                           "and pinned only if it passes and you approve. " if on else "")
                        + f"Then `{model}` comes out of `project.yaml`. Reminders: {', '.join(str(d) for d in REMINDERS)} days before.\n\n"
                        f"**If an agent fails on `{repl}`** it stays on `{model}` for now, and on {date} its calls get 410 Gone. "
                        "Before then, choose one:\n"
                        f"1. Improve the agent (instructions, tools, its evaluation set if it no longer fits) and evaluate it on `{repl}` again.\n"
                        f"2. Ask for an extension: a pull request in the platform repository that moves `{model}`'s retirement date, "
                        "with the reason; the AI platform and model risk decide.\n"
                        "3. Retire the agent.\n\n"
                        "The release posts each failed agent here, and the retirement pull request on the date lists the projects still on it.")
                r = requests.post(api, headers=headers, json={"title": title, "body": body}, timeout=30)
                r.raise_for_status()
                no = r.json()["number"]
                existing[title] = no
                open_issues.add(no)
                state = "notified"
            else:
                state = "issue exists"
            if repl not in allowed:
                url, kind = propose(project, p, repl, model, no, rev)
            else:
                url, kind = propose_cleanup(project, model, repl, no, on)
            announce(api, headers, no, repl if repl not in allowed else f"{model}-retirement", url, kind,
                     what=None if repl not in allowed else f"moving off `{model}` before it retires")
            # Reminder: the nearest threshold reached, once, while the issue is open.
            due = [d for d in REMINDERS if left <= d]
            if no in open_issues and due and left >= 0:
                t = min(due)
                marker = f"<!-- retirement-reminder {model} {t} -->"
                comments = requests.get(f"{api}/{no}/comments", headers=headers, timeout=30, params={"per_page": 100}).json()
                if not any(marker in (c.get("body") or "") for c in comments):
                    requests.post(f"{api}/{no}/comments", headers=headers, timeout=30, json={"body":
                        f"{marker}\n**Reminder: `{model}` retires in {left} days ({date}).** "
                        + (f"Agents still on it: {', '.join(f'`{a}`' for a in on)}. " if on else "")
                        + f"`{model}` is still in `project.yaml`."})
                    state += f", reminder {t} days"
            rows.append(f"| {model} (retiring {date}) | {project} | #{no} ({state}) | {proposal_row(url, kind)} |")
    return rows


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
    existing, open_issues, page = {}, set(), 1
    while True:
        batch = requests.get(api, headers=headers, params={"state": "all", "per_page": 100, "page": page}, timeout=30)
        if batch.status_code == 410:
            sys.exit("Issues are turned off in this repository (Settings > General > Features > Issues).")
        batch.raise_for_status()
        items = batch.json()
        existing.update({i["title"]: i["number"] for i in items if "pull_request" not in i})
        open_issues.update(i["number"] for i in items if "pull_request" not in i and i["state"] == "open")
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
        if (reg.get(baseline) or {}).get("lifecycle") in ("retiring", "retired"):
            continue   # the baseline's retirement notice (below) tells its projects, with this model as the move
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
                rows.append(f"| {cand} | {project} | #{no} | {proposal_row(url, moved)} |")
                announce(api, headers, no, cand, url, moved)
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
            announce(api, headers, no, cand, url, moved)
            rows.append(f"| {cand} | {project} | opened [#{no}]({r.json()['html_url']}) | {proposal_row(url, moved)} |")
    rows += retirements(reg, api, headers, existing, open_issues)
    summary("### Model adoption (from the gateway registry)\n\n| Validated model | Project | Issue | Proposal |\n|---|---|---|---|\n"
            + ("\n".join(rows) or "| (no validated model replaces one that a project uses) | | | |"))


def limits(model):
    """Other projects' tokens per minute on a model (named values model-limit-<project>-<model>)."""
    r = arm("GET", "namedValues", params={"$filter": "startswith(name,'model-limit-')", "$top": "100"}).json()
    out = {}
    for nv in r.get("value", []):
        v = json.loads(nv["properties"].get("value") or "{}")
        if v.get("model") == model:
            out[v.get("project")] = int(v.get("tokens_per_minute") or 0)
    return out


def available(project):
    """After a release pinned agents onto a model this project is still evaluating (at the evaluating cap): propose
    making it available, with a working share of the model's capacity. The share is the AI platform's decision
    (project.yaml is theirs): the proposal starts from the share of the model it replaces, within what's free."""
    from agent_pipeline import STATE, load_project, model_of   # noqa: E402
    outcome = {}
    f = os.path.join(STATE, f"{project}-register.json")
    if os.path.exists(f):
        with open(f, encoding="utf-8") as fh:
            outcome = json.load(fh)
    p = load_project(project)
    pf = os.path.join(PROJECTS, project, "project.yaml")
    with open(pf, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    allowed = {m["name"]: m for m in raw["models"]["allowed"]}
    reg = registry()
    rows = []
    for model, m in allowed.items():
        if m.get("status") != "evaluating":
            continue
        pinned = [a["name"] for a in p["agents"] if model_of(a) == model and (outcome.get(a["name"]) or {}).get("pinned")
                  and (outcome.get(a["name"]) or {}).get("reason") is None]
        if not pinned:
            continue
        branch = f"available/{model}-{project}"
        found = existing_proposal(branch)
        if found is not False:
            rows.append((model, found[0]))
            continue
        entry = reg.get(model) or {}
        baseline = ((entry.get("governance") or {}).get("lifecycle") or {}).get("replaces")
        base = allowed.get(baseline) or {}
        others = {k: v for k, v in limits(model).items() if k != project}
        free = int(entry.get("capacity_tpm") or 0) - sum(others.values())
        want_tpm = int(base.get("tokens_per_minute") or 3 * int(m["tokens_per_minute"]))
        tpm = max(int(m["tokens_per_minute"]), min(want_tpm, free)) if free > 0 else int(m["tokens_per_minute"])
        tpd = int(base.get("tokens_per_day") or 20 * tpm)
        git("checkout", "-q", "-B", branch, "origin/main")
        with open(pf, encoding="utf-8") as fh:
            lines = fh.readlines()
        start = next(i for i, l in enumerate(lines) if re.match(rf"^\s*- name: {re.escape(model)}\b", l))
        indent = len(lines[start]) - len(lines[start].lstrip())
        end = start + 1
        while end < len(lines) and lines[end].strip() and len(lines[end]) - len(lines[end].lstrip()) > indent:
            end += 1
        pad = " " * (indent + 2)
        block = [lines[start]] + [l for l in lines[start + 1:end] if not re.match(r"^\s*(status|tokens_per_minute|tokens_per_day):", l)]
        block += [f"{pad}tokens_per_minute: {tpm:<18}# available (default status): agents {', '.join(pinned)} serve on it\n",
                  f"{pad}tokens_per_day: {tpd}\n"]
        lines[start:end] = block
        with open(pf, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        body = (f"**For the AI platform** (opened by `{os.environ.get('GITHUB_WORKFLOW', 'agent-deploy')}` after a release). "
                f"`{project}`'s agents {', '.join(f'`{a}`' for a in pinned)} passed against their live versions, were "
                f"approved by the use-case owner and now **serve on `{model}`**, which `{project}` still has as "
                f"**evaluating** ({int(m['tokens_per_minute']):,} tokens per minute, the cap for evaluations).\n\n"
                "| | |\n|---|---|\n"
                f"| Proposed | `{model}` **available**, {tpm:,} tokens per minute, {tpd:,} per day |\n"
                f"| Why this share | {'the share of `' + baseline + '` it replaces (' + format(want_tpm, ',') + ')' if base else 'three times the evaluating cap'}, within what's free |\n"
                f"| Model capacity | {int(entry.get('capacity_tpm') or 0):,}; other projects {sum(others.values()):,}; free {free:,} |\n"
                f"| Release | {os.environ.get('RUN_URL', '')} |\n\n"
                f"Capacity is shared across projects, so the AI platform (code owner of `project.yaml`) decides the share: "
                f"change it here if needed, then approve. `{baseline or 'The previous model'}` stays allowed for rollback "
                "until it's retired.")
        url, kind = open_proposal(branch, [pf], f"{project}: make {model} available", body, draft=False)
        rows.append((model, url))
        # On the adoption issue, if there is one.
        title = f"Evaluate model {model} for project {project}"
        api = f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/issues"
        headers = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}
        issues = requests.get(api, headers=headers, timeout=30, params={"state": "open", "per_page": 100}).json()
        no = next((i["number"] for i in issues if i.get("title") == title), None)
        if no:
            how = "**[open the pull request]({})**".format(url) if kind == "open" else url
            requests.post(f"{api}/{no}/comments", headers=headers, timeout=30, json={"body":
                f"<!-- available-proposal {model} -->\nThe agents now serve on `{model}`. Proposal to make it "
                f"available with a working share ({tpm:,} tokens per minute), for the AI platform to approve: {how}"})
    if rows:
        summary("### Proposed: make models available\n\n| Model | Proposal |\n|---|---|\n"
                + "\n".join(f"| {m} | {u or 'pull request exists'} |" for m, u in rows))


def held_on_retiring(project):
    """After a release: agents that failed on a replacement and stay on a retiring model get a comment on the
    project's retirement issue, with the date and the options."""
    from agent_pipeline import STATE, load_project, model_of   # noqa: E402
    f = os.path.join(STATE, f"{project}-register.json")
    if not os.path.exists(f):
        return
    with open(f, encoding="utf-8") as fh:
        outcome = json.load(fh)
    held = {a: o for a, o in outcome.items() if o.get("reason") == "failed evaluation"}
    if not held:
        return
    reg = registry()
    p = load_project(project)
    api = f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/issues"
    headers = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}
    issues = requests.get(api, headers=headers, timeout=30, params={"state": "open", "per_page": 100}).json()
    for old, entry in reg.items():
        if entry.get("lifecycle") != "retiring":
            continue
        ret = (((entry.get("governance") or {}).get("lifecycle") or {}).get("retirement") or {})
        repl, date = ret.get("replacement"), ret.get("date")
        agents = [a["name"] for a in p["agents"] if a["name"] in held and model_of(a) == repl]
        if not agents:
            continue
        title = f"Model {old} retires on {date}: move to {repl} ({project})"
        no = next((i["number"] for i in issues if i.get("title") == title), None)
        if not no:
            continue
        run = os.environ.get("RUN_URL", "")
        requests.post(f"{api}/{no}/comments", headers=headers, timeout=30, json={"body":
            f"**{', '.join(f'`{a}`' for a in agents)} failed on `{repl}`** ([release]({run})) and "
            f"{'stay' if len(agents) > 1 else 'stays'} on `{old}`, which retires on **{date}**: from then on "
            f"{'their' if len(agents) > 1 else 'its'} calls get 410 Gone.\n\nChoose before the date:\n"
            f"1. Improve the agent and evaluate it on `{repl}` again (a new pull request).\n"
            f"2. Ask for an extension: a pull request in the platform repository moving `{old}`'s retirement date, with the reason.\n"
            "3. Retire the agent."})
        print(f"{project}: {', '.join(agents)} held on retiring {old}; posted on #{no}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["available"]:
        available(sys.argv[2])   # after a release (agent-deploy publish)
    elif sys.argv[1:2] == ["held"]:
        held_on_retiring(sys.argv[2])   # after a release: agents that failed on a retiring model's replacement
    else:
        main(*sys.argv[1:])
