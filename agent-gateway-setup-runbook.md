# Agent Gateway Build Instructions — APM Lookup Agent

**Audience:** a coding agent (Claude Code / similar) executing this build, supervised by a DevOps engineer.
**Goal:** deploy an agent on Vertex AI Agent Engine with Agent Identity, route all its traffic through Agent Gateway, and let it query Cloud SQL for PostgreSQL via the Google-managed Cloud SQL MCP server — with registry, IAM/IAP (audit mode), Model Armor (inspect mode), and database-level controls in place.

---

## Ground rules for the executing agent

1. **Execute phases strictly in order.** Later phases depend on earlier ones. Do not parallelize across phases.
2. **Run every VERIFY block** after its phase and confirm the expected result before moving on. If verification fails, fix that phase — do not proceed on top of a broken step.
3. **Stop conditions — halt and ask the human when:**
   - a step is marked `HUMAN ACTION REQUIRED`;
   - any command asks you to delete, or to overwrite a resource you did not create in this run;
   - a verification fails twice after your best fix;
   - you reach Phase 10 (enforcement flip) — it is gated on a human decision after ~1 week of audit data.
4. **Never** run destructive SQL (`DROP`, `DELETE`, `TRUNCATE`, `ALTER` on existing objects), never remove existing IAM bindings (only add), never disable an API, never patch a Cloud SQL instance without first reading its current flags (Phase 2 explains why).
5. **CLI drift handling:** these products are new (2026) and flags move. Before running any `gcloud` command you haven't run yet in this session, you may run it with `--help` to confirm the flags exist. If a documented command errors with an unknown-flag/unknown-command, consult the doc linked in that phase, adapt, and note the change in your final report.
6. **Region consistency is a hard requirement:** gateway, agent, and Model Armor templates must be in the same `$REGION`. Model Armor cannot be called cross-region. Agent Registry location must be a specific region (not `us`/`eu` multi-region).
7. **Idempotency:** `import` commands replace the resource's config. Re-importing a YAML you generated in this run is safe; importing over a resource that already existed before this run is not — stop and ask.
8. **Final report:** when you stop (end of Phase 9, or any stop condition), produce a summary: what was created (with resource names), what was verified, what deviated from these instructions, and what remains for the human.

---

## Phase 0 — Inputs (human fills these in BEFORE sending to the agent)

Do not guess these values. If any placeholder below is unfilled, stop and ask.

```bash
export PROJECT_ID="FILL_ME"            # target GCP project
export ORG_ID="FILL_ME"                # GCP organization ID
export REGION="us-central1"            # ONE region for everything
export SQL_INSTANCE="FILL_ME"          # existing Cloud SQL for PostgreSQL instance name
export DB_NAME="FILL_ME"               # database containing the APM table
export APM_TABLE="FILL_ME"             # e.g. public.apm_assets
export GATEWAY_NAME="apm-agent-gateway"
export AGENT_NAME="apm_lookup_agent"

# Derived — compute, don't ask:
export PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
```

Also confirm with the human before starting:
- [ ] Does the Cloud SQL instance already serve production traffic? (affects Phase 2 caution level)
- [ ] Is ingress governance wanted too (client→agent gateway), or egress only? (Phase 4 optional block)

VERIFY:
```bash
gcloud config set project $PROJECT_ID
gcloud projects describe $PROJECT_ID --format='value(projectId)'   # expect: $PROJECT_ID
echo $PROJECT_NUMBER                                               # expect: non-empty number
gcloud sql instances describe $SQL_INSTANCE --format='value(name,region,databaseVersion)'
# expect: instance exists, region == $REGION (if region differs, flag to human — cross-region SQL works but adds latency), POSTGRES_*
```

---

## Phase 1 — Enable APIs

```bash
gcloud services enable \
  compute.googleapis.com \
  networksecurity.googleapis.com \
  networkservices.googleapis.com \
  dns.googleapis.com \
  iam.googleapis.com \
  agentregistry.googleapis.com \
  aiplatform.googleapis.com \
  discoveryengine.googleapis.com \
  storage.googleapis.com \
  modelarmor.googleapis.com \
  observability.googleapis.com \
  telemetry.googleapis.com \
  monitoring.googleapis.com \
  cloudtrace.googleapis.com \
  logging.googleapis.com \
  apphub.googleapis.com \
  apptopology.googleapis.com \
  cloudapiregistry.googleapis.com \
  sqladmin.googleapis.com \
  iap.googleapis.com \
  dlp.googleapis.com \
  --project=$PROJECT_ID
```

Why `sqladmin.googleapis.com` matters twice: it is the Cloud SQL Admin API **and** the host of the Google-managed Cloud SQL MCP server (`https://sqladmin.googleapis.com/mcp`). Per Google docs, enabling a supported API **auto-registers its managed MCP server and tools in Agent Registry** — so Phase 3 verifies rather than creates.

VERIFY:
```bash
for s in networkservices agentregistry modelarmor sqladmin aiplatform iap; do
  gcloud services list --enabled --filter="name:${s}.googleapis.com" --format='value(name)'
done
# expect: each prints its service name. Any empty line = that API failed to enable.
```

Doc: https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/set-up-agent-gateway

---

## Phase 2 — Prepare Cloud SQL

### 2.1 Enable IAM database authentication — READ FLAGS FIRST

`--database-flags` **replaces the entire flag set**. Blindly patching wipes existing flags. So:

```bash
# 1. Read current flags:
gcloud sql instances describe $SQL_INSTANCE \
  --format='value(settings.databaseFlags)'

# 2. Build the patch INCLUDING every existing flag plus the new one, e.g. if current
#    flags are max_connections=200, run:
gcloud sql instances patch $SQL_INSTANCE \
  --database-flags=max_connections=200,cloudsql.iam_authentication=on
# If there are no existing flags:
gcloud sql instances patch $SQL_INSTANCE \
  --database-flags=cloudsql.iam_authentication=on
```

If the instance is production (per Phase 0 answer): warn the human that a flag patch may restart the instance, and get an explicit go-ahead before patching.

### 2.2 Data API access — HUMAN ACTION REQUIRED

The MCP `execute_sql` tool requires instance setting `data_api_access = ALLOW_DATA_API`. There is no classic gcloud flag for it. Ask the human to set it: **Console → SQL → instance → Edit → Data API access → Allow** (or via the MCP server's own `update_instance` tool once MCP access works).

VERIFY (after human confirms):
```bash
gcloud sql instances describe $SQL_INSTANCE --format=json | grep -i -A2 "dataApi"
# expect: ALLOW_DATA_API. If the field name differs, grep the full describe output for "data".
```

### 2.3 Database user + GRANTs — deferred

The agent's identity doesn't exist yet. Do this in Phase 6.3. Note it now; don't skip it later.

Doc: https://docs.cloud.google.com/sql/docs/postgres/use-cloudsql-mcp · https://docs.cloud.google.com/sql/docs/postgres/create-edit-iam-instances

---

## Phase 3 — Verify Agent Registry contents

The Cloud SQL MCP server should have auto-registered when its API was enabled.

VERIFY:
```bash
gcloud agent-registry services list --location=$REGION --project=$PROJECT_ID
# expect: an entry for the Cloud SQL MCP server.
# If the command group differs (CLI drift), try: gcloud agent-registry --help
# Fallback: Console → Agent Registry → MCP servers tab (ask human to confirm + paste the
# registry resource path, needed in Phase 4).
```

Record two values for later phases:
- `REGISTRY_PATH` — the registry resource path (format like `projects/$PROJECT_ID/locations/$REGION/registries/<name>`; read it from the list output or console page).
- `MCP_SERVER_ID` — the registered Cloud SQL MCP server's ID (for the narrow IAM grant in Phase 6).

Endpoint the agent will call — use the **narrowest toolset** that supports `execute_sql`:
- Preferred: `https://sqladmin.googleapis.com/mcp/query_execution`
- Read-only browsing toolset: `https://sqladmin.googleapis.com/mcp/readonly`
- Do NOT use the full `https://sqladmin.googleapis.com/mcp` surface.

Rule to remember: once the gateway is live, **all egress to anything not in the registry is blocked by default**. Everything the agent will call must appear here.

Doc: https://docs.cloud.google.com/agent-registry/register-mcp-servers

---

## Phase 4 — Create the Agent Gateway (egress)

Write `my-agent-gateway-egress.yaml` (substitute `REGISTRY_PATH` from Phase 3):

```yaml
name: apm-agent-gateway
protocols:
  - MCP
googleManaged:
  governedAccessPath: AGENT_TO_ANYWHERE
registries:
  - REGISTRY_PATH
```

```bash
gcloud network-services agent-gateways import $GATEWAY_NAME \
  --source="my-agent-gateway-egress.yaml" \
  --location=$REGION
```

**Optional (only if Phase 0 said ingress is wanted):** second gateway, ingress mode. Constraint: agent must be in the same project and region.

```yaml
# my-agent-gateway-ingress.yaml
name: apm-agent-gateway-ingress
protocols:
  - MCP
googleManaged:
  governedAccessPath: CLIENT_TO_AGENT
```

```bash
gcloud network-services agent-gateways import apm-agent-gateway-ingress \
  --source="my-agent-gateway-ingress.yaml" \
  --location=$REGION
```

VERIFY:
```bash
gcloud network-services agent-gateways describe $GATEWAY_NAME --location=$REGION
# expect: state ACTIVE (or equivalent ready state), governedAccessPath AGENT_TO_ANYWHERE,
# registries listing REGISTRY_PATH.
```

Record: `GATEWAY_RESOURCE="projects/$PROJECT_ID/locations/$REGION/agentGateways/$GATEWAY_NAME"`

Doc: https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/set-up-agent-gateway

---

## Phase 5 — Build and deploy the agent

### 5.1 Scaffold

```bash
pip install uv
uvx google-agents-cli setup
uvx google-agents-cli create apm-agent-project --prototype --yes
cd apm-agent-project
mv app $AGENT_NAME
```

### 5.2 Agent code

Write `$AGENT_NAME/agent.py` as an ADK agent that:
- takes an APM ID from the user prompt;
- has ONE tool: the MCP toolset pointed at the Phase 3 endpoint (`.../mcp/query_execution`);
- builds a **parameterized** query against `$APM_TABLE` filtered on the APM ID — never interpolate the raw user string into SQL;
- returns the rows in a readable summary.

Keep the agent minimal. No other tools. (Semantic policies added in Phase 10 assume this shape: SELECT-only, filtered on APM ID, no external-send tools.)

### 5.3 Deploy with Agent Identity + gateway attached

Deploy with the Vertex AI SDK so identity and gateway are set in one config (the deployment config is where both live):

```python
from vertexai import types

remote_agent = client.agent_engines.create(
    agent=local_agent,
    config={
        "agent_gateway_config": {
            "agent_to_anywhere_config": {
                "agent_gateway": "projects/PROJECT_ID/locations/REGION/agentGateways/apm-agent-gateway"
            }
            # if ingress gateway exists, also:
            # "client_to_agent_config": {"agent_gateway": "projects/PROJECT_ID/locations/REGION/agentGateways/apm-agent-gateway-ingress"}
        },
        "identity_type": types.IdentityType.AGENT_IDENTITY,
        "env_vars": {
            "GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES": False,
        },
    },
)
print(remote_agent.resource_name)   # capture: ends in reasoningEngines/ENGINE_ID
```

(Alternative CLI path: `echo '{ "identity_type": "AGENT_IDENTITY" }' > $AGENT_NAME/.agent_engine_config.json` then `uv run adk deploy agent_engine $AGENT_NAME --project="$PROJECT_ID" --region="$REGION"` — but then the gateway must be attached afterwards with the PATCH below.)

**Attaching the gateway to an already-deployed agent:**
```bash
curl -X PATCH \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  "https://$REGION-aiplatform.googleapis.com/v1/projects/$PROJECT_ID/locations/$REGION/reasoningEngines/ENGINE_ID?updateMask=spec.deploymentSpec.agentGatewayConfig" \
  -d '{"spec":{"deploymentSpec":{"agentGatewayConfig":{"agentToAnywhereConfig":{"agentGateway":"projects/'$PROJECT_ID'/locations/'$REGION'/agentGateways/'$GATEWAY_NAME'"}}}}}'
```

### 5.4 Capture the agent principal — HUMAN ACTION REQUIRED (console copy)

Ask the human: **Console → Vertex AI → Agent Engine → Deployments → Identity column → copy the full string** for this agent. Format:

```
principal://agents.global.org-ORG_ID.system.id.goog/resources/aiplatform/projects/PROJECT_NUMBER/locations/REGION/reasoningEngines/ENGINE_ID
```

```bash
export AGENT_PRINCIPAL="<pasted value>"
```

VERIFY:
```bash
curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://$REGION-aiplatform.googleapis.com/v1/projects/$PROJECT_ID/locations/$REGION/reasoningEngines/ENGINE_ID" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('name')); print(json.dumps(d.get('spec',{}).get('deploymentSpec',{}).get('agentGatewayConfig',{}), indent=2))"
# expect: the engine name, and agentGatewayConfig showing the gateway resource path.
echo "$AGENT_PRINCIPAL" | grep -q "^principal://agents" && echo PRINCIPAL_OK
```

Docs: https://docs.cloud.google.com/iam/docs/create-and-deploy-agent · https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/agent-gateway-runtime-deploy

---

## Phase 6 — IAM for the agent identity

### 6.1 Gateway egress authorization: `roles/iap.egressor`

The gateway checks exactly one permission on egress — `iap.webServiceVersions.egressViaIAP` — and **only** `roles/iap.egressor` carries it.

Write `agents-iap-policy.json`:

```json
{
  "bindings": [
    {
      "role": "roles/iap.egressor",
      "members": ["AGENT_PRINCIPAL_VALUE"]
    }
  ]
}
```

⚠️ `set-iam-policy` **replaces** the policy on that resource. First fetch the existing policy and merge your binding into it — never drop existing bindings:

```bash
gcloud iap web get-iam-policy \
  --project=$PROJECT_ID --resource-type=agent-registry --region=$REGION > current-policy.json
# merge the egressor binding into current-policy.json, then:
gcloud iap web set-iam-policy current-policy.json \
  --project=$PROJECT_ID --resource-type=agent-registry --region=$REGION
```

(Scope note: this is registry-wide, acceptable for the audit phase. Phase 10 narrows it with `--mcp-server=$MCP_SERVER_ID`.)

### 6.2 Database role

```bash
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="$AGENT_PRINCIPAL" \
  --role="roles/cloudsql.instanceUser"
```

If `execute_sql` later fails with a permission error despite this, add `roles/cloudsql.studioUser` (documented as carrying MCP query-tool permissions) — do NOT jump to `cloudsql.admin`.

### 6.3 IAM database user + GRANTs (deferred from Phase 2)

```bash
# The DB username drops the .gserviceaccount.com suffix (Postgres 63-char limit):
# sa-name@project-id.iam.gserviceaccount.com  →  "sa-name@project-id.iam"
gcloud sql users create SERVICE_ACCT_WITHOUT_SUFFIX \
  --instance=$SQL_INSTANCE \
  --type=cloud_iam_service_account
```

Then connect (`gcloud sql connect $SQL_INSTANCE --database=$DB_NAME` as an admin user, or hand the SQL to the human if you lack DB admin credentials — HUMAN ACTION if so):

```sql
GRANT CONNECT ON DATABASE apm TO "sa-name@project-id.iam";
GRANT USAGE ON SCHEMA public TO "sa-name@project-id.iam";
GRANT SELECT ON public.apm_assets TO "sa-name@project-id.iam";
-- NOTHING ELSE. This GRANT is the real write-protection: execute_sql will run
-- any SQL the DB user is allowed, so the DB user must only be allowed SELECT on this table.
```

Also confirm the APM ID column is indexed (`\d apm_assets`) — `execute_sql` has a 30s timeout.

### 6.4 Containment: deny policy + PAB

`deny-agent-destructive.json`:
```json
{
  "displayName": "Deny destructive Cloud SQL ops for the APM agent",
  "rules": [
    {
      "denyRule": {
        "deniedPrincipals": ["AGENT_PRINCIPAL_VALUE"],
        "deniedPermissions": [
          "sqladmin.googleapis.com/instances.delete",
          "sqladmin.googleapis.com/instances.update",
          "sqladmin.googleapis.com/databases.delete"
        ]
      }
    }
  ]
}
```
```bash
gcloud iam policies create deny-agent-destructive \
  --attachment-point=cloudresourcemanager.googleapis.com/projects/$PROJECT_ID \
  --kind=denypolicies \
  --policy-file=deny-agent-destructive.json
```

PAB (limits what the agent can ever reach):
```bash
gcloud beta iam principal-access-boundary-policies create apm-agent-pab \
  --organization=$ORG_ID --location=global \
  --details-rules="[{\"description\":\"limit agent to project\",\"resources\":[\"//cloudresourcemanager.googleapis.com/projects/$PROJECT_ID\"],\"effect\":\"ALLOW\"}]" \
  --details-enforcement-version=1
```
Binding the PAB to the agent principal set is easiest in console — HUMAN ACTION REQUIRED: **Console → IAM → Principal Access Boundary → apm-agent-pab → add binding** for the agent principal.

VERIFY:
```bash
gcloud iap web get-iam-policy --project=$PROJECT_ID --resource-type=agent-registry --region=$REGION \
  | grep -A2 "iap.egressor"          # expect: agent principal listed
gcloud projects get-iam-policy $PROJECT_ID \
  --flatten="bindings[].members" \
  --filter="bindings.members:agents.global" \
  --format="table(bindings.role)"     # expect: roles/cloudsql.instanceUser
gcloud sql users list --instance=$SQL_INSTANCE | grep -i iam   # expect: the agent DB user
gcloud iam policies list --attachment-point=cloudresourcemanager.googleapis.com/projects/$PROJECT_ID --kind=denypolicies
```

Doc: https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/policies/configure-iam-policies

---

## Phase 7 — Enforcement wiring in DRY_RUN

`iap-request-authz-extension.yaml`:
```yaml
name: apm-gateway-authz-ext
service: iap.googleapis.com
failOpen: true
timeout: 1s
metadata:
  iamEnforcementMode: "DRY_RUN"
  iapPolicyVersion: "V1"
```
```bash
gcloud beta service-extensions authz-extensions import apm-gateway-authz-ext \
  --source=iap-request-authz-extension.yaml --location=$REGION
```

`iap-request-authz-policy.yaml` (substitute real PROJECT_ID/REGION):
```yaml
name: apm-gateway-authz-policy
target:
  resources:
    - "projects/PROJECT_ID/locations/REGION/agentGateways/apm-agent-gateway"
policyProfile: REQUEST_AUTHZ
action: CUSTOM
customProvider:
  authzExtension:
    resources:
      - "projects/PROJECT_ID/locations/REGION/authzExtensions/apm-gateway-authz-ext"
```
```bash
gcloud network-security authz-policies import apm-gateway-authz-policy \
  --source=iap-request-authz-policy.yaml --location=$REGION
```

`DRY_RUN` = IAP evaluates every call and logs would-be denials without blocking. Phase 9 reads those logs.

VERIFY:
```bash
gcloud beta service-extensions authz-extensions describe apm-gateway-authz-ext --location=$REGION
gcloud network-security authz-policies describe apm-gateway-authz-policy --location=$REGION
# expect: both exist; extension metadata shows DRY_RUN; policy target = the gateway resource.
```

---

## Phase 8 — Model Armor (inspect-only, same region)

### 8.1 Egress template (screens rows returning from Postgres — the critical direction)

```bash
gcloud model-armor templates create apm-egress-template \
  --project=$PROJECT_ID --location=$REGION \
  --rai-settings-filters='[{"filterType":"HATE_SPEECH","confidenceLevel":"MEDIUM_AND_ABOVE"},{"filterType":"HARASSMENT","confidenceLevel":"MEDIUM_AND_ABOVE"},{"filterType":"DANGEROUS","confidenceLevel":"MEDIUM_AND_ABOVE"},{"filterType":"SEXUALLY_EXPLICIT","confidenceLevel":"MEDIUM_AND_ABOVE"}]' \
  --pi-and-jailbreak-filter-settings-enforcement=enabled \
  --pi-and-jailbreak-filter-settings-confidence-level=MEDIUM_AND_ABOVE \
  --malicious-uri-filter-settings-enforcement=enabled \
  --basic-config-filter-enforcement=enabled \
  --template-metadata-log-operations \
  --template-metadata-log-sanitize-operations
```

### 8.2 Ingress template (only if the ingress gateway exists)

```bash
gcloud model-armor templates create apm-ingress-template \
  --project=$PROJECT_ID --location=$REGION \
  --pi-and-jailbreak-filter-settings-enforcement=enabled \
  --pi-and-jailbreak-filter-settings-confidence-level=MEDIUM_AND_ABOVE \
  --malicious-uri-filter-settings-enforcement=enabled \
  --basic-config-filter-enforcement=enabled \
  --template-metadata-log-operations
```

### 8.3 Redaction is NOT automatic — set up advanced SDP

`basic-config-filter` only detects/blocks a fixed infoType set; it never rewrites content. For masked rows:

HUMAN ACTION REQUIRED (SDP templates are console/API): **Console → Security → Sensitive Data Protection → Configuration → Templates**, create:
- inspect template `apm-inspect` — infoTypes present in APM data (e.g. `EMAIL_ADDRESS`, `PHONE_NUMBER`, `US_SOCIAL_SECURITY_NUMBER`);
- de-identify template `apm-deidentify` — masking/replace transformation.

Then attach both:

```bash
gcloud model-armor templates update apm-egress-template \
  --project=$PROJECT_ID --location=$REGION \
  --advanced-config-inspect-template="projects/$PROJECT_ID/locations/$REGION/inspectTemplates/apm-inspect" \
  --advanced-config-deidentify-template="projects/$PROJECT_ID/locations/$REGION/deidentifyTemplates/apm-deidentify"
```

Inspect template alone = findings reported. Inspect + de-identify = content returned redacted. Both = required for redaction.

### 8.4 Attach templates to the gateway — HUMAN ACTION REQUIRED

**Console → Agent Gateway → gateway → Model Armor**: enable, select `apm-egress-template` on the egress gateway (and `apm-ingress-template` on the ingress one), accept the prompted role grants (egress runs via the Service Extensions service agent; ingress via the AI Platform Reasoning Engine service agent). Choose **inspect-only** mode.

VERIFY:
```bash
gcloud model-armor templates describe apm-egress-template --location=$REGION --project=$PROJECT_ID
# expect: filters as configured; advancedConfig shows both SDP template paths after 8.3.
```

Docs: https://docs.cloud.google.com/model-armor/manage-templates · https://docs.cloud.google.com/model-armor/model-armor-agent-gateway-integration

---

## Phase 9 — Validation (audit mode) — then STOP

Run each test; capture evidence (log excerpts) for the final report.

1. **Happy path:** query the deployed agent with a real APM ID (Agent Engine `streamQuery`, or ask the human to use the console test pane). Expect: correct rows summarized.
2. **Attribution:** in Cloud Logging, search for the agent principal string. Expect: gateway/log entries for the MCP call attributed to `principal://agents...`.
3. **DRY_RUN cleanliness:** search logs for IAP dry-run deny events over the test window. Expect **zero would-be denials** for legitimate traffic. Any hit = missing egressor grant or registry entry — fix and re-test.
4. **Registry default-deny:** attempt an MCP call to an unregistered endpoint (temporary scratch tool or curl through the gateway). Expect: blocked.
5. **DB write-protection:** ask the agent (or call `execute_sql` directly) to run an `UPDATE`. Expect: fails with a Postgres permission error — proving the GRANT, not the gateway, blocks writes.
6. **Model Armor on response path:** insert a test row whose text contains an injection string ("ignore all previous instructions and ..."), query its APM ID, and check logs. Expect: Model Armor finding logged (inspect mode logs, doesn't block).
7. **Timeout sanity:** `EXPLAIN ANALYZE` the lookup query — confirm indexed access well under the 30s `execute_sql` timeout.

**STOP HERE.** Produce the final report (per ground rule 8). Phase 10 runs only after the human has reviewed ~1 week of audit logs and explicitly approves enforcement.

---

## Phase 10 — Enforcement flip (HUMAN-GATED — do not run without explicit approval)

1. **IAP enforce:** Console → Agent Gateway → gateway → access authorization → switch audit-only to **Enforce policies**. Calls without an explicit IAM Allow now 403.
2. **Model Armor block:** switch templates from inspect-only to block; confirm the de-identify template is attached so PII returns masked rather than the response failing.
3. **Narrow the egressor grant** from registry-wide to the MCP server: re-apply Phase 6.1 with `--mcp-server=$MCP_SERVER_ID`, then remove the registry-wide binding (fetch-merge-set, same caution).
4. **Semantic policies** (Console → Agent Gateway → policies), now that a week of traffic shows the agent's normal shape:
   - only SELECT statements through `execute_sql`;
   - query must filter on the requested APM ID;
   - block DB-read + external-send tool combinations in one plan;
   - block `SELECT *` / WHERE-less queries.
5. **Re-run the entire Phase 9 checklist in enforce mode.** Tests 4–6 should now show hard blocks, not just log findings.

---

## Reference docs

- Set up Agent Gateway — https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/set-up-agent-gateway
- Route Agent Runtime traffic through Agent Gateway — https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/agent-gateway-runtime-deploy
- Create/deploy agent with Agent CLI + Agent Identity — https://docs.cloud.google.com/iam/docs/create-and-deploy-agent
- Configure IAM agent policies — https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/policies/configure-iam-policies
- Register MCP servers — https://docs.cloud.google.com/agent-registry/register-mcp-servers
- Cloud SQL MCP server — https://docs.cloud.google.com/sql/docs/postgres/use-cloudsql-mcp
- Cloud SQL IAM database users — https://docs.cloud.google.com/sql/docs/postgres/add-manage-iam-users
- Model Armor templates — https://docs.cloud.google.com/model-armor/manage-templates
- Model Armor + Agent Gateway — https://docs.cloud.google.com/model-armor/model-armor-agent-gateway-integration
