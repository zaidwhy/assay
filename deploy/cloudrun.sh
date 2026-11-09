#!/usr/bin/env bash
# Deploy assay to Google Cloud Run from source (Cloud Build builds the Dockerfile; no local Docker).
#
#   bash deploy/cloudrun.sh <gcp-project-id> [region]
#
# Needs: gcloud signed in, the project's billing enabled, GROQ_API_KEY and TAVILY_API_KEY in the
# environment (on Windows they are read from the user environment if this shell does not have them).
# Keys go to Secret Manager and are never printed or passed on a command line.
set -euo pipefail

PROJECT="${1:?usage: bash deploy/cloudrun.sh <gcp-project-id> [region]}"
REGION="${2:-asia-south1}"
SERVICE=assay

GCLOUD=gcloud
if ! command -v gcloud >/dev/null 2>&1; then
  GCLOUD="${LOCALAPPDATA:-$HOME/AppData/Local}/Google/Cloud SDK/google-cloud-sdk/bin/gcloud.cmd"
fi
gc() { "$GCLOUD" --project "$PROJECT" --quiet "$@"; }

user_env() {
  # A key set with [Environment]::SetEnvironmentVariable(..., "User") is not in shells started earlier.
  local name="$1"
  if [ -n "${!name:-}" ]; then printf '%s' "${!name}"; return; fi
  if command -v powershell >/dev/null 2>&1; then
    powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('$name','User')" | tr -d '\r\n'
  fi
}

billing=$(gc billing projects describe "$PROJECT" --format='value(billingEnabled)' 2>/dev/null || true)
if [ "$billing" != "True" ]; then
  echo "Billing is not enabled on project $PROJECT. Link a billing account at"
  echo "  https://console.cloud.google.com/billing/linkedaccount?project=$PROJECT"
  echo "then run this script again. A demo this size stays inside Cloud Run's free tier."
  exit 1
fi

echo "Enabling services..."
gc services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com

put_secret() {
  local secret="$1" value="$2"
  if [ -z "$value" ]; then echo "missing value for $secret" >&2; exit 1; fi
  if gc secrets describe "$secret" >/dev/null 2>&1; then
    printf '%s' "$value" | gc secrets versions add "$secret" --data-file=- >/dev/null
  else
    printf '%s' "$value" | gc secrets create "$secret" --replication-policy=automatic --data-file=- >/dev/null
  fi
  echo "stored secret $secret"
}
put_secret assay-groq-api-key "$(user_env GROQ_API_KEY)"
put_secret assay-tavily-api-key "$(user_env TAVILY_API_KEY)"

number=$(gc projects describe "$PROJECT" --format='value(projectNumber)')
runtime_sa="${number}-compute@developer.gserviceaccount.com"
for secret in assay-groq-api-key assay-tavily-api-key; do
  gc secrets add-iam-policy-binding "$secret" --member="serviceAccount:$runtime_sa" \
    --role=roles/secretmanager.secretAccessor >/dev/null
done

# CPU stays allocated between requests (--no-cpu-throttling) because a run keeps working after
# POST /research has already returned 202. --timeout covers a long-lived event stream. One instance:
# run state and the SQLite file live in the process, so a second instance would 404 on its runs.
echo "Building and deploying (Cloud Build, a few minutes the first time)..."
gc run deploy "$SERVICE" --source . --region "$REGION" \
  --memory 2Gi --cpu 1 --no-cpu-throttling --min-instances 0 --max-instances 1 \
  --concurrency 16 --timeout 900 --allow-unauthenticated \
  --set-secrets GROQ_API_KEY=assay-groq-api-key:latest,TAVILY_API_KEY=assay-tavily-api-key:latest

url=$(gc run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo "Deployed: $url"
curl -s "$url/health" | head -c 300; echo
