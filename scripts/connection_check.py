"""Architecture check: can a project's model connection be bent to reach models it isn't allowed to use?

  connection_check.py <project> <other project>

Runs in <project>'s Foundry project (default platform-eval) with temporary connections and a temporary prompt agent,
all deleted at the end. Each row says what a project admin could try, what the gateway must do, and what happened:

  1. control       the project's own connection and an allowed model                  must answer
  2. other API     a connection pointing at <other project>'s model API              must be refused (401)
  3. not allowed   the project's own API, but a model only <other project> may use    must be refused (404)
  4. bypass        the pipeline identity calls the model account directly, no gateway  reported (who can do this)

Rows 2 and 3 prove that "each project uses only its own models" is enforced by the gateway, not by the connection: a
connection is configuration any project admin can edit. Row 4 shows whether the model account can be reached without
the gateway by an identity with a role on it.

Sign-in: Azure CLI. The identity needs Reader on APIM and the Foundry account, and on the project: write connections,
Foundry User (the same as tool_smoke.py).
"""
import json
import os
import sys
import time

import requests
import yaml
from azure.identity import AzureCliCredential

from platform_env import environment

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARM = "https://management.azure.com"
RUN = f"{os.environ.get('GITHUB_RUN_ID', 'local')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
AGENT = f"connection-check-{RUN}"
CONN_API = "2025-04-01-preview"
cred = AzureCliCredential(process_timeout=60)


def summary(text):
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(text + "\n\n")
    print(text)


def arm(method, path, api_version, body=None):
    token = cred.get_token(f"{ARM}/.default").token
    return requests.request(method, f"{ARM}{path}", params={"api-version": api_version}, json=body,
                            headers={"Authorization": f"Bearer {token}"}, timeout=60)


def project_yaml(name):
    with open(os.path.join(ROOT, "projects", name, "project.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def ask(openai, agent):
    """('answered', text) or ('refused', error) for one agent call."""
    try:
        resp = openai.responses.create(input="Reply with the single word OK.",
                                       extra_body={"agent_reference": {"name": agent, "type": "agent_reference"}})
    except Exception as e:  # the model call failed inside the agent run: the gateway's error surfaces here
        return "refused", str(e)[:220].replace("|", "/").replace("\n", " ")
    if getattr(resp, "status", "completed") == "failed" or getattr(resp, "error", None):
        return "refused", str(getattr(resp, "error", ""))[:220].replace("|", "/").replace("\n", " ")
    return "answered", resp.output_text[:60]


def main(project, other):
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import PromptAgentDefinition

    env = environment()
    g, fd = env["gateway"], env["agents_foundry"]
    p, o = project_yaml(project), project_yaml(other)
    fp = (p.get("foundry") or {}).get("project", project)
    own_conn = (p.get("models") or {}).get("connection", "platform-models")
    own_models = [m["name"] for m in p["models"]["allowed"]]
    other_models = [m["name"] for m in o["models"]["allowed"]]
    shared = next(m for m in own_models if m in other_models)          # allowed for both: only the API differs
    foreign = next((m for m in other_models if m not in own_models), None)  # allowed only for the other project
    gateway = f"https://{g['apim_name']}.azure-api.net"

    account = (f"/subscriptions/{fd['subscription_id']}/resourceGroups/{fd['resource_group']}"
               f"/providers/Microsoft.CognitiveServices/accounts/{fd['account_name']}")
    project_id = f"{account}/projects/{fp}"
    r = arm("GET", f"{project_id}/connections/{own_conn}", CONN_API)
    if r.status_code != 200:
        sys.exit(f"Can't read connection {own_conn} in {fp}: {r.status_code} {r.text[:300]}")
    template = r.json()["properties"]
    model_entry = {m["name"]: m for m in json.loads(template["metadata"]["models"])}
    # The other project's entries too, so a borrowed model is described exactly as Foundry knows it.
    ofp = (o.get("foundry") or {}).get("project", other)
    oc = arm("GET", f"{account}/projects/{ofp}/connections/{(o.get('models') or {}).get('connection', 'platform-models')}",
             CONN_API)
    if oc.status_code == 200:
        for m in json.loads(oc.json()["properties"]["metadata"]["models"]):
            model_entry.setdefault(m["name"], m)

    def temp_connection(name, target, model):
        props = {k: v for k, v in template.items() if k in (
            "category", "authType", "audience", "isSharedToAll", "metadata")}
        props.update({"target": target, "credentials": {},
                      "metadata": {**template["metadata"], "models": json.dumps(
                          [model_entry.get(model) or {"name": model, "properties": {"model": {
                              "name": model, "version": "", "format": "OpenAI"}}}])}})
        r = arm("PUT", f"{project_id}/connections/{name}", CONN_API, {"properties": props})
        if r.status_code not in (200, 201):
            sys.exit(f"Can't create temporary connection {name}: {r.status_code} {r.text[:300]}")
        return name

    client = AIProjectClient(endpoint=f"https://{fd['account_name']}.services.ai.azure.com/api/projects/{fp}",
                             credential=cred)
    openai = client.get_openai_client()
    rows = ["| # | A project admin tries | The gateway must | Result |", "|---|---|---|---|"]
    failed, temps = [], []

    def run(n, what, must, conn, model, expect):
        client.agents.create_version(agent_name=AGENT, definition=PromptAgentDefinition(
            model=f"{conn}/{model}", instructions="Follow the user's instruction exactly."))
        got, detail = ask(openai, AGENT)
        ok = got == expect
        rows.append(f"| {n} | {what} (`{conn}/{model}`) | {must} | {'pass' if ok else 'FAIL'}: {got}: {detail!r} |")
        if not ok:
            failed.append(str(n))

    try:
        run(1, f"its own connection, model `{shared}`", "answer", own_conn, shared, "answered")

        c2 = temp_connection(f"check-other-api-{RUN}"[:60], f"{gateway}/models/{other}", shared)
        temps.append(c2)
        time.sleep(10)
        run(2, f"a connection pointing at `{other}`'s model API", "refuse: not this project's identity (401)",
            c2, shared, "refused")

        if foreign:
            c3 = temp_connection(f"check-not-allowed-{RUN}"[:60], template["target"], foreign)
            temps.append(c3)
            time.sleep(10)
            run(3, f"its own model API with `{foreign}`, which only `{other}` may use",
                "refuse: model not allowed (404)", c3, foreign, "refused")
        else:
            rows.append(f"| 3 | (skipped: `{other}` allows no model that `{project}` doesn't) | | |")
    finally:
        try:
            client.agents.delete(agent_name=AGENT)
        except Exception as e:
            print(f"  (cleanup agent: {e})")
        for c in temps:
            r = arm("DELETE", f"{project_id}/connections/{c}", CONN_API)
            print(f"  (cleanup connection {c}: {r.status_code})")

    # 4. Bypass: the model account straight from this identity, no gateway.
    rg = f"/subscriptions/{fd['subscription_id']}/resourceGroups/{fd['resource_group']}"
    accounts = arm("GET", f"{rg}/providers/Microsoft.CognitiveServices/accounts", "2024-10-01").json().get("value", [])
    hosts = [a for a in accounts if a["name"] != fd["account_name"] and "models" in a["name"]]
    if hosts:
        h = hosts[0]
        public = h["properties"].get("publicNetworkAccess", "Enabled")
        url = f"https://{h['properties'].get('customSubDomainName', h['name'])}.cognitiveservices.azure.com"
        token = cred.get_token("https://cognitiveservices.azure.com/.default").token
        r = requests.post(f"{url}/openai/deployments/{shared}/chat/completions", params={"api-version": "2024-10-21"},
                          headers={"Authorization": f"Bearer {token}"}, timeout=60,
                          json={"messages": [{"role": "user", "content": "Reply with the single word OK."}]})
        verdict = ("reachable WITHOUT the gateway by this identity (it has a role on the model account)"
                   if r.status_code == 200 else f"refused ({r.status_code})")
        rows.append(f"| 4 | the pipeline identity calls `{h['name']}` directly, deployment `{shared}` "
                    f"(public network: {public}) | (reported) | {verdict} |")
    else:
        rows.append("| 4 | (skipped: no model account found in the resource group) | | |")

    summary(f"**Model connection check: `{project}` against `{other}`**\n\n" + "\n".join(rows))
    if failed:
        sys.exit(f"Connection check failed: rows {', '.join(failed)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "platform-eval", sys.argv[2] if len(sys.argv) > 2 else "models-demo")
