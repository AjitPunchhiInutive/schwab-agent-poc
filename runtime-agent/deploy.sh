#!/usr/bin/env bash
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Runtime Validation Agent — Cloud Run Deployment Script
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────
PROJECT_ID="${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"
REGION="${GCP_REGION:-us-east4}"
SERVICE_NAME="runtime-validation-agent"
SERVICE_ACCOUNT="sa-runtime-agent@${PROJECT_ID}.iam.gserviceaccount.com"
REPO="agent-images"
IMAGE_NAME="runtime-validation-agent"
VPC_CONNECTOR="agent-vpc-connector"
TAG="${BUILD_TAG:-$(date +%Y%m%d%H%M%S)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${IMAGE_NAME}:${TAG}"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Runtime Validation Agent — Deployment"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Project         : ${PROJECT_ID}"
echo "  Region          : ${REGION}"
echo "  Service         : ${SERVICE_NAME}"
echo "  Image           : ${IMAGE}"
echo "  Service Account : ${SERVICE_ACCOUNT}"
echo "  VPC Connector   : ${VPC_CONNECTOR}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── Step 1: Create Artifact Registry (if not exists) ─────────────────
echo ""
echo "Step 1/5: Ensuring Artifact Registry repository exists..."
gcloud artifacts repositories describe "${REPO}" \
  --location="${REGION}" \
  --project="${PROJECT_ID}" \
  --quiet 2>/dev/null \
|| gcloud artifacts repositories create "${REPO}" \
  --repository-format=docker \
  --location="${REGION}" \
  --project="${PROJECT_ID}" \
  --description="Runtime validation agent container images" \
  --quiet

# ── Step 2: Build and push container image ───────────────────────────
echo ""
echo "Step 2/5: Building and pushing container image..."
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

docker build \
  --platform=linux/amd64 \
  --tag="${IMAGE}" \
  --tag="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${IMAGE_NAME}:latest" \
  .

docker push "${IMAGE}"
docker push "${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${IMAGE_NAME}:latest"

echo "  ✅ Image pushed: ${IMAGE}"

# ── Step 3: Create VPC Connector (if not exists) ─────────────────────
echo ""
echo "Step 3/5: Ensuring VPC Connector exists..."
gcloud compute networks vpc-access connectors describe "${VPC_CONNECTOR}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --quiet 2>/dev/null \
|| gcloud compute networks vpc-access connectors create "${VPC_CONNECTOR}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --network="default" \
  --range="10.8.0.0/28" \
  --min-instances=2 \
  --max-instances=10 \
  --quiet

# ── Step 4: Deploy to Cloud Run ──────────────────────────────────────
echo ""
echo "Step 4/5: Deploying to Cloud Run..."
gcloud run deploy "${SERVICE_NAME}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --service-account="${SERVICE_ACCOUNT}" \
  --vpc-connector="${VPC_CONNECTOR}" \
  --vpc-egress=private-ranges-only \
  --no-allow-unauthenticated \
  --ingress=internal-only \
  --memory=512Mi \
  --cpu=1 \
  --min-instances=1 \
  --max-instances=10 \
  --timeout=60 \
  --concurrency=80 \
  --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},AGENT_ENV=prod" \
  --labels="app=runtime-agent,team=cloud-engineering,env=prod" \
  --quiet

# ── Step 5: Verify deployment ────────────────────────────────────────
echo ""
echo "Step 5/5: Verifying deployment..."
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format="value(status.url)")

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✅ Deployment Complete"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Service URL : ${SERVICE_URL}"
echo "  Image       : ${IMAGE}"
echo "  Region      : ${REGION}"
echo "  Auth        : IAM required (no public access)"
echo "  Ingress     : Internal only"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  Test with:"
echo "    curl -H \"Authorization: Bearer \$(gcloud auth print-identity-token)\" \\"
echo "      ${SERVICE_URL}/healthz"
echo ""
