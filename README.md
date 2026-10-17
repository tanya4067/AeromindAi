# AeroMind AI Enterprise Prototype (0.3)

A multi-tab dark aerospace operations dashboard with:
- Introduction and operations overview
- Gemini on Vertex AI structured visual inspection from uploaded images
- User-initiated browser camera snapshot analysis (not unattended continuous detection)
- Dated synthetic demand records, seven-day recent-average forecast, safety-stock/reorder calculations
- Machine sensor risk demo
- Workplace safety observations and team-level capacity planning
- Sustainability and waste-stream analysis
- Cross-functional operational insights and draft action workflow
- Optional Cloud Storage image upload, BigQuery event logging, and Pub/Sub event publishing

## Local run (Mac)
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```
Set `GOOGLE_CLOUD_PROJECT` in `.env`, then authenticate Application Default Credentials:
```bash
gcloud auth application-default login
```
Enable Vertex AI API in your Google Cloud project and grant the authenticated identity permission to call Vertex AI. Then load environment variables:
```bash
set -a
source .env
set +a
uvicorn main:app --reload
```
Open http://127.0.0.1:8000.

If you have not configured GCP credentials yet, the dashboard and synthetic-data features load, but image inspection returns a configuration error until Vertex AI is configured.

## Optional GCP integrations
- Cloud Storage: set `GCS_BUCKET` and grant the Cloud Run service account object-creation permissions.
- BigQuery: create a table with columns `event_id STRING`, `event_type STRING`, `event_time TIMESTAMP`, `payload_json STRING`; set `BQ_DATASET` and `BQ_TABLE`.
- Pub/Sub: set `PUBSUB_TOPIC` to a topic name and grant publish permissions.
- Vertex AI: set project, region and model env vars; enable Vertex AI API.

Keep `.env` out of source control. Use Secret Manager for deployed credentials/secrets and the Cloud Run service identity for IAM. Never put service-account JSON or keys in frontend code.

## Deploy
Use Cloud Run source deployment from this directory or build the Dockerfile. Configure `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `GEMINI_MODEL`, and optional integration variables in Cloud Run. Use Secret Manager for any secret values. Give the service identity least-privilege IAM.

## Important limits
- Included operational data is synthetic and clearly for demo analysis.
- Supply forecasting is a simple recent-average baseline, not a validated time-series model.
- Machine risk is a transparent rule-based demo, not validated failure probability.
- Camera feature analyzes only user-captured frames after explicit action; it is not continuous unattended real-time defect detection.
- Workforce capacity is a team-level planning estimate, not an individual performance score.
- Safety and quality recommendations are advisory. No real machinery, purchase orders, worker monitoring, or maintenance ticket systems are controlled.
- Before production, add authentication, audit controls, access policies, data retention, monitoring, model evaluation and domain safety review.

## Prototype 0.4 additions
- **Agentic Operations Copilot** (`POST /api/copilot`): sends the user's question and a bounded sample of synthetic operational records to Gemini on Vertex AI when configured. If Gemini is unavailable, a clearly labeled deterministic fallback returns evidence-linked recommendations. The fallback does not interpret the free-text question as an LLM would.
- **What-if Production Simulator** (`POST /api/simulate`): models simple effects of machine outage, demand change, workforce absence, supplier delay, scrap, material and energy assumptions. This is scenario arithmetic, not a validated digital twin.
- Both endpoints emit optional BigQuery and Pub/Sub events when configured.

## Firebase Hosting (optional frontend split)
The included app serves its static UI from FastAPI, so Cloud Run alone is the simplest deployment. If you specifically want Firebase Hosting, split `static/index.html` into a frontend deployment and configure Firebase Hosting rewrites or frontend API calls to the HTTPS Cloud Run service. Do not expose cloud credentials in browser code. For a plain static frontend, use a Firebase project, install Firebase CLI (`npm install -g firebase-tools`), run `firebase login`, initialize Hosting in the frontend directory (`firebase init hosting`), set the public API base URL to your Cloud Run URL, then deploy with `firebase deploy --only hosting`. Verify CORS and API routing if the frontend and backend have different origins.
