#!/usr/bin/env bash
# deploy.sh — Build and deploy IRCTC Vacancy MCP to Google Cloud Run
# Usage: bash deploy.sh [--project PROJECT_ID]
#
# Requirements:
#   - gcloud CLI installed and authenticated
#   - Docker installed (for local builds) OR Cloud Build will be used
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
PROJECT="${GCLOUD_PROJECT:-notebooklm-pplx-mcp}"
REGION="us-central1"
SERVICE="irctc-vacancy-mcp"
IMAGE="gcr.io/${PROJECT}/${SERVICE}"
MEMORY="1Gi"
CPU="1"
MIN_INSTANCES="0"   # scale to zero when idle (free tier)
MAX_INSTANCES="3"

echo ""
echo "=== IRCTC Vacancy MCP — Cloud Run Deploy ==="
echo "  Project : ${PROJECT}"
echo "  Region  : ${REGION}"
echo "  Service : ${SERVICE}"
echo "  Image   : ${IMAGE}"
echo ""

# ── Step 1: Set active project ────────────────────────────────────────────────
echo "[1/5] Setting active GCP project..."
gcloud config set project "${PROJECT}"

# ── Step 2: Enable required APIs ─────────────────────────────────────────────
echo "[2/5] Enabling Cloud Run + Artifact Registry APIs..."
gcloud services enable run.googleapis.com cloudbuild.googleapis.com containerregistry.googleapis.com --quiet

# ── Step 3: Build using Cloud Build (no local Docker needed) ─────────────────
echo "[3/5] Building container image via Cloud Build..."
gcloud builds submit \
  --tag "${IMAGE}:latest" \
  --timeout=20m \
  .

# ── Step 4: Deploy to Cloud Run ───────────────────────────────────────────────
echo "[4/5] Deploying to Cloud Run..."
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}:latest" \
  --platform managed \
  --region "${REGION}" \
  --allow-unauthenticated \
  --memory "${MEMORY}" \
  --cpu "${CPU}" \
  --min-instances "${MIN_INSTANCES}" \
  --max-instances "${MAX_INSTANCES}" \
  --port 8080 \
  --timeout 300 \
  --concurrency 10 \
  --quiet

# ── Step 5: Print stable URL ──────────────────────────────────────────────────
echo "[5/5] Getting stable URL..."
URL=$(gcloud run services describe "${SERVICE}" \
  --region "${REGION}" \
  --format="value(status.url)")

echo ""
echo "=== ✓ Deployment complete! ==="
echo ""
echo "  Stable MCP URL (add /sse for Perplexity):"
echo "  ${URL}/sse"
echo ""
echo "  Add this as your Perplexity connector:"
echo "  MCP Server URL : ${URL}/sse"
echo "  Transport      : SSE"
echo "  Auth           : None"
echo ""
echo "  After adding, push your IRCTC cookie via Perplexity:"
echo "    'Call set_irctc_cookie with my cookie: <paste cookie here>'"
echo ""
