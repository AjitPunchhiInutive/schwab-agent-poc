"""
Runtime Validation Agent — Deterministic APM Validation Pipeline

Authenticate → Fetch APM ID → Query PostgreSQL → Return Result

This agent uses Google ADK for orchestration, session management,
and observability — but follows a DETERMINISTIC flow with no LLM
reasoning. Each tool executes in a fixed sequence.
"""

import logging
from google.adk.agents import Agent
from google.adk.tools import FunctionTool

from tools.authenticator import authenticate_endpoint
from tools.apm_fetcher import fetch_apm_id
from tools.db_validator import validate_apm_in_database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ─── Tool Definitions ────────────────────────────────────────────────────────
# Each tool maps to one step of the deterministic pipeline.

authenticate_tool = FunctionTool(func=authenticate_endpoint)
fetch_apm_tool = FunctionTool(func=fetch_apm_id)
validate_db_tool = FunctionTool(func=validate_apm_in_database)


# ─── Agent Definition ────────────────────────────────────────────────────────

AGENT_INSTRUCTION = """
You are a DETERMINISTIC runtime validation agent. You do NOT reason,
infer, or make probabilistic decisions. You execute a FIXED pipeline.

## Pipeline (execute in this EXACT order — no exceptions)

### Step 1 — Authenticate
Call authenticate_endpoint with:
  - project_id (from user input)
  - auth_method (from user input, default "oauth2")

If status is "AUTH_FAILED" → STOP. Return the error immediately.

### Step 2 — Fetch APM ID
Call fetch_apm_id with:
  - endpoint_url (from Step 1 result)
  - token (from Step 1 result)
  - auth_method (from Step 1 result)

If status is "APM_FETCH_FAILED" → STOP. Return the error immediately.

### Step 3 & 4 — Validate Against PostgreSQL
Call validate_apm_in_database with:
  - apm_id (from Step 2 result)
  - project_id (from user input)
  - expected_application (from Step 2 result)
  - expected_environment (from Step 2 result)

### Step 5 — Return Result
Return the COMPLETE result from Step 4. Include:
  - Validation status (VALID / INVALID / NOT_FOUND / DB_QUERY_FAILED)
  - APM ID
  - All checks performed with PASS/FAIL/WARN
  - Database record details (if found)
  - Query execution time
  - Any errors encountered

## Rules
- NEVER skip a step.
- NEVER reorder steps.
- NEVER retry a failed step unless explicitly asked.
- NEVER modify data — this agent is READ-ONLY.
- Always pass outputs from one step as inputs to the next.
- If ANY step fails, STOP and return the failure details immediately.
"""

# ─── Create the Agent ────────────────────────────────────────────────────────

root_agent = Agent(
    model="gemini-2.5-flash",
    name="runtime_validation_agent",
    description=(
        "Deterministic runtime agent that authenticates against a configured "
        "endpoint, retrieves the APM ID, and cross-validates it against a "
        "PostgreSQL database. Fixed pipeline — no LLM reasoning involved."
    ),
    instruction=AGENT_INSTRUCTION,
    tools=[
        authenticate_tool,
        fetch_apm_tool,
        validate_db_tool,
    ],
)
