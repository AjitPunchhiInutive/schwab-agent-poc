# COPILOT PROMPT — Runtime Validation Agent Deployment

## Context

You are building and deploying a **deterministic runtime validation agent** to **Agent Platform → Agents → Deployments** on Google Cloud.

## Skills available

Read and use the skills in `.agent/skills/active/` — they contain the canonical ADK patterns for scaffolding, coding, evaluation, and deployment. Specifically:

- `google-agents-cli-workflow` — the full lifecycle (scaffold → build → eval → deploy)
- `google-agents-cli-adk-code` — ADK code patterns, SequentialAgent, tools
- `google-agents-cli-scaffold` — project structure and templates
- `google-agents-cli-deploy` — deployment to Agent Runtime
- `google-agents-cli-eval` — evaluation harness

**Read each relevant skill file before writing any code or running any command.**

## Runbook

Read `apm-agent-project/RUNBOOK.md` — it is the authoritative source for the Agent Gateway, IAM, Model Armor, and Cloud SQL MCP wiring. Follow its phases in strict order.

## Infrastructure (already provisioned via Terraform)

These resources already exist in project `schwab-agent-poc`. DO NOT recreate them:

```
Cloud SQL Instance:   schwab-agent-poc:us-east4:apm-validation-db
Database:             apm_db
VPC Network:          projects/schwab-agent-poc/global/networks/agent-vpc
VPC Subnet:           agent-subnet (10.0.0.0/24)
VPC Connector:        agent-vpc-connector
Service Account:      sa-runtime-agent@schwab-agent-poc.iam.gserviceaccount.com
Artifact Registry:    us-east4-docker.pkg.dev/schwab-agent-poc/agent-images
Firewall:             deny-all-ingress (priority 65534), allow-internal-postgres (port 5432)
Secrets (created):    apm-endpoint-url, apm-client-id, apm-client-secret
State Bucket:         gs://itp-terraform-test/schwab-agent-poc/state
```

## What you need to build

A deterministic agent that executes a **fixed sequence** — no LLM decides the order:

```
Step 1: Authenticate    → Call APM endpoint with credentials from Secret Manager
Step 2: Fetch APM ID    → Retrieve APM ID from authenticated endpoint
Step 3: Query PostgreSQL → Validate APM ID against apm_registry table in apm_db
Step 4: Return Result   → VALID / INVALID / NOT_FOUND with check details
```

### Architecture

```
SequentialAgent (root_agent)                    ← NOT powered by LLM
  ├── step1_authenticate (LlmAgent)             ← Tool: authenticate_endpoint
  │   output_key: "auth_result"                 ← Saved to shared state
  ├── step2_fetch_apm (LlmAgent)                ← Tool: fetch_apm_id
  │   output_key: "apm_result"                  ← Reads auth_result from state
  └── step3_validate_db (LlmAgent)              ← Tool: validate_apm_in_database
      output_key: "validation_result"           ← Reads apm_result from state
```

The `SequentialAgent` executes sub-agents in strict order. It is deterministic — always step1 → step2 → step3. Data flows between steps via shared session state using `output_key`.

### Database connection

Connect to Cloud SQL using the Cloud SQL Python Connector with IAM authentication (no passwords):

```python
from google.cloud.sql.connector import Connector

connector = Connector()
conn = await connector.connect_async(
    "schwab-agent-poc:us-east4:apm-validation-db",
    "asyncpg",
    user="sa-runtime-agent@schwab-agent-poc.iam",
    db="apm_db",
    enable_iam_auth=True,
)
```

### Database schema

```sql
CREATE TABLE IF NOT EXISTS apm_registry (
    id              SERIAL PRIMARY KEY,
    apm_id          VARCHAR(255) NOT NULL UNIQUE,
    application     VARCHAR(255) NOT NULL,
    environment     VARCHAR(50)  NOT NULL,
    status          VARCHAR(50)  NOT NULL DEFAULT 'ACTIVE',
    owner           VARCHAR(255),
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_apm_id ON apm_registry (apm_id);
```

### Validation checks (Step 3)

1. `record_exists` — APM ID found in apm_registry table
2. `status_active` — status column equals 'ACTIVE'
3. `application_match` — application matches what the endpoint returned
4. `environment_match` — environment matches what the endpoint returned

If any check is FAIL → overall status is INVALID.
If record not found → NOT_FOUND.
If DB connection fails → DB_QUERY_FAILED.
If all checks PASS → VALID.

## Execution plan — follow these steps in order

### Phase A — Read skills and runbook

1. Read `.agent/skills/active/google-agents-cli-workflow/` skill files
2. Read `.agent/skills/active/google-agents-cli-adk-code/` skill files
3. Read `.agent/skills/active/google-agents-cli-deploy/` skill files
4. Read `apm-agent-project/RUNBOOK.md`
5. Confirm you understand the architecture before proceeding

### Phase B — Write agent code

1. Navigate to `apm-agent-project/`
2. Write `apm_lookup_agent/agent.py` with:
   - Secret Manager helper (read credentials at runtime)
   - Tool 1: `authenticate_endpoint` — OAuth2/API key/bearer auth
   - Tool 2: `fetch_apm_id` — GET request to APM endpoint
   - Tool 3: `validate_apm_in_database` — Cloud SQL Connector + asyncpg query
   - Three LlmAgent sub-agents with output_key for state passing
   - SequentialAgent root_agent wiring step1 → step2 → step3
   - App wrapper: `app = App(root_agent=root_agent, name="runtime-validation-agent")`
3. Write `apm_lookup_agent/__init__.py` exporting `root_agent`
4. Update `pyproject.toml` adding dependencies:
   - google-cloud-secret-manager>=2.20.0
   - cloud-sql-python-connector[asyncpg]>=1.12.0
   - asyncpg>=0.29.0
   - httpx>=0.27.0
5. Update `.env` with schwab-agent-poc config

### Phase C — Local test

1. Run `agents-cli install` to install dependencies
2. Run `agents-cli playground` to start local testing at localhost:8080
3. Test with: "Validate APM for project schwab-agent-poc"
4. Verify the three steps execute in order in the trace

### Phase D — Runbook Phase 0 (inputs)

Set these variables — they are already known:

```bash
export PROJECT_ID="schwab-agent-poc"
export ORG_ID="203589767236"
export REGION="us-east4"
export SQL_INSTANCE="apm-validation-db"
export DB_NAME="apm_db"
export APM_TABLE="public.apm_registry"
export GATEWAY_NAME="apm-agent-gateway"
export AGENT_NAME="apm_lookup_agent"
export PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
```

### Phase E — Runbook Phase 1 (APIs)

Execute the API enablement from the runbook. Verify each API is enabled.

### Phase F — Runbook Phase 2 (Cloud SQL prep)

- Read existing database flags before patching (CRITICAL — flags replace, not merge)
- IAM authentication is already enabled (Terraform set `cloudsql.iam_authentication=on`)
- STOP and ask human for Data API access setting (HUMAN ACTION REQUIRED)

### Phase G — Runbook Phase 3 (Agent Registry)

Verify Cloud SQL MCP server is auto-registered. Record REGISTRY_PATH and MCP_SERVER_ID.

### Phase H — Runbook Phase 4 (Agent Gateway)

Create the egress gateway YAML and import it. Verify state is ACTIVE.

### Phase I — Deploy with Agent Identity

Deploy using the Vertex AI SDK with Agent Identity and gateway attached:

```python
from vertexai import types

remote_agent = client.agent_engines.create(
    agent=local_agent,
    config={
        "agent_gateway_config": {
            "agent_to_anywhere_config": {
                "agent_gateway": "projects/schwab-agent-poc/locations/us-east4/agentGateways/apm-agent-gateway"
            }
        },
        "identity_type": types.IdentityType.AGENT_IDENTITY,
        "env_vars": {
            "GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES": False,
        },
    },
)
```

STOP and ask human to copy the Agent Principal from Console → Vertex AI → Agent Engine → Deployments → Identity column.

### Phase J — Runbook Phase 6 (IAM)

- Grant `roles/iap.egressor` on the agent principal (fetch-merge-set, never overwrite)
- Grant `roles/cloudsql.instanceUser` to the agent principal
- Create IAM database user (trimsuffix .gserviceaccount.com)
- GRANT SELECT ONLY on apm_registry
- Create deny policy for destructive SQL ops
- Create PAB to limit agent to project scope

### Phase K — Runbook Phases 7-8 (enforcement + Model Armor in DRY_RUN)

- IAP authz extension in DRY_RUN mode
- Model Armor egress template (inspect-only)
- STOP for human to attach templates in console

### Phase L — Runbook Phase 9 (Validation)

Run all 7 validation tests from the runbook. Produce the final report.

**STOP after Phase L. Do NOT execute Phase 10 (enforcement flip) without explicit human approval.**

## Rules

1. Follow phases in strict order. Do not skip or parallelize.
2. Run every VERIFY block and confirm before proceeding.
3. STOP and ask human at every HUMAN ACTION REQUIRED step.
4. Never run destructive SQL (DROP, DELETE, TRUNCATE, ALTER).
5. Never remove existing IAM bindings — only add.
6. Never disable an API.
7. All resources must be in us-east4 (region consistency is mandatory).
8. Before any new gcloud command, run it with --help first to verify flags exist.
9. Produce a final report: what was created, what was verified, what deviated, what remains.
