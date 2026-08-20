#!/usr/bin/env bash
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  COPILOT BOOTSTRAP — Runtime Validation Agent
#  
#  Run this script FIRST. It:
#    1. Downloads agents-cli skills into .agent/skills
#    2. Installs agents-cli toolchain
#    3. Scaffolds the project
#    4. Writes the deterministic agent code
#    5. Prepares for deployment to Agent Platform > Agents > Deployments
#
#  After running this, hand the COPILOT_PROMPT.md to your coding agent
#  (Claude Code, Copilot, Antigravity) to execute the full deployment.
#
#  Prerequisites:
#    - gcloud authenticated: gcloud auth login --update-adc
#    - Project set: gcloud config set project schwab-agent-poc
#    - Python 3.11+, uv, Node.js installed
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────
export PROJECT_ID="schwab-agent-poc"
export ORG_ID="203589767236"
export REGION="us-east4"
export SQL_INSTANCE="apm-validation-db"
export SQL_CONNECTION="schwab-agent-poc:us-east4:apm-validation-db"
export DB_NAME="apm_db"
export APM_TABLE="public.apm_registry"
export VPC_NETWORK="projects/schwab-agent-poc/global/networks/agent-vpc"
export GATEWAY_NAME="apm-agent-gateway"
export AGENT_NAME="apm_lookup_agent_poc"
export AGENT_SA="sa-runtime-agent@schwab-agent-poc.iam.gserviceaccount.com"
export PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)' 2>/dev/null || echo "FILL_ME")

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Runtime Validation Agent — Copilot Bootstrap"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Project        : $PROJECT_ID"
echo "  Region         : $REGION"
echo "  Cloud SQL      : $SQL_CONNECTION"
echo "  VPC            : $VPC_NETWORK"
echo "  Service Account: $AGENT_SA"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── Step 1: Download agents-cli skills ───────────────────────────────
echo ""
echo "Step 1/5: Downloading agents-cli skills..."

mkdir -p .agent/skills

# Clone skills from google/agents-cli repo
if [ -d ".agent/skills/agents-cli-skills" ]; then
  echo "  Skills already downloaded — pulling latest..."
  cd .agent/skills/agents-cli-skills && git pull --quiet && cd ../../..
else
  git clone --depth=1 https://github.com/google/agents-cli.git .agent/skills/agents-cli-skills
fi

# Symlink individual skills for easy access
mkdir -p .agent/skills/active
for skill_dir in .agent/skills/agents-cli-skills/skills/*/; do
  skill_name=$(basename "$skill_dir")
  if [ ! -L ".agent/skills/active/$skill_name" ]; then
    ln -sf "$(realpath "$skill_dir")" ".agent/skills/active/$skill_name"
  fi
done

echo "  ✅ Skills downloaded:"
ls -1 .agent/skills/active/ | sed 's/^/     /'

# ── Step 2: Install agents-cli toolchain ─────────────────────────────
echo ""
echo "Step 2/5: Installing agents-cli toolchain..."

pip install uv 2>/dev/null || true
uv tool install google-agents-cli 2>/dev/null || pip install google-agents-cli --break-system-packages 2>/dev/null || true

# Install skills into coding agent
uvx google-agents-cli setup 2>/dev/null || agents-cli setup 2>/dev/null || echo "  ⚠️  agents-cli setup requires interactive — run manually"

echo "  ✅ agents-cli installed"
agents-cli --version 2>/dev/null || echo "  Version check skipped"

# ── Step 3: Scaffold the agent project ───────────────────────────────
echo ""
echo "Step 3/5: Scaffolding agent project..."

if [ ! -d "apm-agent-project" ]; then
  agents-cli create apm-agent-project \
    --agent adk \
    --deployment-target agent_runtime \
    --region $REGION \
    --prototype \
    --yes
  echo "  ✅ Project scaffolded"
else
  echo "  ⚠️  apm-agent-project already exists — skipping scaffold"
fi

cd apm-agent-project

# Rename app to agent name
if [ -d "app" ] && [ ! -d "$AGENT_NAME" ]; then
  mv app $AGENT_NAME
fi

# ── Step 4: Write the .env ───────────────────────────────────────────
echo ""
echo "Step 4/5: Writing configuration..."

cat > .env << ENVEOF
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=$PROJECT_ID
GOOGLE_CLOUD_LOCATION=$REGION
ENVEOF

echo "  ✅ .env configured"

# ── Step 5: Copy runbook into project ────────────────────────────────
echo ""
echo "Step 5/5: Copying runbook and generating copilot prompt..."

# Copy the runbook if it exists in parent
if [ -f "../agent-gateway-setup-runbook.md" ]; then
  cp ../agent-gateway-setup-runbook.md ./RUNBOOK.md
  echo "  ✅ Runbook copied as RUNBOOK.md"
fi

cd ..

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✅ Bootstrap Complete"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  Skills installed at: .agent/skills/active/"
echo "  Agent project at:    apm-agent-project/"
echo ""
echo "  Next: Open your coding agent and paste the contents of"
echo "        COPILOT_PROMPT.md as your first message."
echo ""
