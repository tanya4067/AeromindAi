import base64, csv, io, json, os, uuid
from collections import defaultdict
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image

APP_DIR = Path(__file__).parent
DATA_DIR = APP_DIR / "data"
STATIC_DIR = APP_DIR / "static"
app = FastAPI(title="AeroMind AI Operations Intelligence", version="0.3.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

def env_flag(name: str) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes"}

def load_csv(name):
    p = DATA_DIR / name
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def get_vertex_client():
    # Vertex AI mode: set GOOGLE_CLOUD_PROJECT and enable Vertex AI API.
    # For local demo, ADC can be initialized with `gcloud auth application-default login`.
    from google import genai
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    if not project:
        raise RuntimeError("Set GOOGLE_CLOUD_PROJECT to use Gemini on Vertex AI.")
    return genai.Client(vertexai=True, project=project, location=location)

def call_gemini_image(picture: Image.Image, prompt: str):
    from google.genai import types
    client = get_vertex_client()
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    response = client.models.generate_content(
        model=model,
        contents=[prompt, picture],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
        ),
    )
    try:
        return json.loads(response.text or "{}"), model
    except json.JSONDecodeError:
        return {"overall_observation": response.text or "No response", "possible_issues": [],
                "recommended_next_steps": ["Qualified human review required."],
                "human_review_required": True}, model

def log_bigquery(event_type, payload):
    """Best-effort event logging. Configure BQ_DATASET and BQ_TABLE to enable."""
    project, dataset, table = (os.getenv("GOOGLE_CLOUD_PROJECT"), os.getenv("BQ_DATASET"), os.getenv("BQ_TABLE"))
    if not (project and dataset and table):
        return "BigQuery logging not configured; result returned live only."
    try:
        from google.cloud import bigquery
        client = bigquery.Client(project=project)
        row = {"event_id": str(uuid.uuid4()), "event_type": event_type,
               "event_time": datetime.now(timezone.utc).isoformat(),
               "payload_json": json.dumps(payload, default=str)}
        errors = client.insert_rows_json(f"{project}.{dataset}.{table}", [row])
        if errors:
            return f"BigQuery insert warning: {errors[0]}"
        return "Event logged to BigQuery."
    except Exception as exc:
        return f"BigQuery logging unavailable: {type(exc).__name__}"

def publish_pubsub(event_type, payload):
    project, topic = os.getenv("GOOGLE_CLOUD_PROJECT"), os.getenv("PUBSUB_TOPIC")
    if not (project and topic):
        return "Pub/Sub not configured; event handled synchronously."
    try:
        from google.cloud import pubsub_v1
        publisher = pubsub_v1.PublisherClient()
        future = publisher.publish(publisher.topic_path(project, topic),
                                   json.dumps({"event_type": event_type, "payload": payload, "event_time": datetime.now(timezone.utc).isoformat()}).encode())
        future.result(timeout=5)
        return "Event published to Pub/Sub."
    except Exception as exc:
        return f"Pub/Sub publish unavailable: {type(exc).__name__}"

def upload_cloud_storage(filename: str, raw: bytes, content_type: str):
    bucket_name = os.getenv("GCS_BUCKET")
    if not bucket_name:
        return None, "Cloud Storage not configured; image was processed in memory only."
    try:
        from google.cloud import storage
        bucket = storage.Client().bucket(bucket_name)
        object_name = f"aeromind/{date.today().isoformat()}/{uuid.uuid4().hex}_{Path(filename).name}"
        blob = bucket.blob(object_name)
        blob.upload_from_string(raw, content_type=content_type)
        return f"gs://{bucket_name}/{object_name}", "Image stored in Cloud Storage."
    except Exception as exc:
        return None, f"Cloud Storage upload unavailable: {type(exc).__name__}"

@app.get("/")
def dashboard():
    return FileResponse(str(STATIC_DIR / "index.html"))

@app.get("/health")
def health():
    return {"status": "ok", "version": "0.3.0", "ai_mode": "Vertex AI Gemini", "timestamp": datetime.now(timezone.utc).isoformat()}

@app.get("/api/demo-data")
def demo_data():
    inventory = load_csv("inventory.csv")
    demand = load_csv("demand_history.csv")
    sensors = load_csv("sensor_readings.csv")
    safety = load_csv("safety_observations.csv")
    waste = load_csv("waste_log.csv")
    return {"inventory": inventory, "demand_history": demand, "sensor_readings": sensors,
            "safety_observations": safety, "waste_log": waste,
            "generated_data": True, "data_note": "Synthetic demonstration dataset, not live factory data."}

@app.get("/api/overview")
def overview():
    inventory = load_csv("inventory.csv")
    demand = load_csv("demand_history.csv")
    sensors = load_csv("sensor_readings.csv")
    safety = load_csv("safety_observations.csv")
    waste = load_csv("waste_log.csv")
    shortages = 0
    for item in inventory:
        comp = item["component_id"]
        hist = [int(r["quantity"]) for r in demand if r["component_id"] == comp]
        avg = sum(hist[-14:]) / max(1, len(hist[-14:]))
        projected = int(item["current_stock"]) + int(item["incoming_quantity"]) - round(avg * 7)
        if projected < int(item["safety_stock"]): shortages += 1
    high_sensor = sum(1 for x in sensors if x["event_level"] == "HIGH")
    open_safety = sum(1 for x in safety if x["status"] != "CLOSED")
    waste_kg = round(sum(float(x["waste_kg"]) for x in waste), 1)
    return {"components_monitored": len(inventory), "components_at_risk": shortages,
            "high_sensor_events": high_sensor, "open_safety_observations": open_safety,
            "waste_kg_period": waste_kg, "data_status": "SYNTHETIC DEMO DATA"}

@app.post("/api/inspect")
async def inspect(image: UploadFile = File(...), checklist: str = Form(default="Check visible defects, missing fasteners, surface damage and image quality."), source: str = Form(default="upload")):
    if image.content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(415, "Upload a JPG, PNG, or WEBP image.")
    raw = await image.read()
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(413, "Image must be 8 MB or smaller.")
    try:
        picture = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        raise HTTPException(400, "Could not read image.")
    prompt = f"""
You are an advisory visual assistant for a non-flight-critical mock manufacturing component.
Inspection checklist: {checklist}
Return ONLY JSON with keys:
overall_observation (string),
possible_issues (array of objects with issue, visual_evidence, severity LOW/MEDIUM/HIGH/UNCERTAIN,
confidence LOW/MEDIUM/HIGH, location_description),
checklist_results (array of objects with item, result OBSERVED/NOT_OBSERVED/UNCERTAIN, notes),
image_quality (object with adequate_for_review boolean and limitations array),
recommended_next_steps (array of strings),
human_review_required (true).
Describe only visible evidence. Do not invent exact coordinates or measurements. An empty issue list does not prove no defect.
Always require qualified human review. This is not an airworthiness assessment.
"""
    try:
        report, model = call_gemini_image(picture, prompt)
    except Exception as exc:
        raise HTTPException(502, f"Vertex AI request failed: {type(exc).__name__}. Check project, credentials, API enablement, and model access.")
    gcs_uri, storage_status = upload_cloud_storage(image.filename or "inspection.jpg", raw, image.content_type)
    event = {"filename": image.filename, "source": source, "model": model, "report": report, "gcs_uri": gcs_uri}
    bq_status = log_bigquery("visual_inspection", event)
    pub_status = publish_pubsub("visual_inspection", {"filename": image.filename, "source": source, "issues": len(report.get("possible_issues", []))})
    return {"inspection_id": str(uuid.uuid4())[:8].upper(), "source": source, "model": model,
            "image_dimensions": {"width": picture.width, "height": picture.height}, "report": report,
            "storage_status": storage_status, "storage_uri": gcs_uri, "analytics_status": bq_status,
            "event_status": pub_status,
            "safety_note": "Advisory only. Not for flight-critical or airworthiness decisions."}

@app.post("/api/inspect-frame")
async def inspect_frame(payload: dict):
    # Browser sends a JPEG data URL from the user's camera only after explicit click.
    data_url = payload.get("image_data_url", "")
    if not data_url.startswith("data:image/"):
        raise HTTPException(400, "A camera image is required.")
    try:
        encoded = data_url.split(",", 1)[1]
        raw = base64.b64decode(encoded)
        picture = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        raise HTTPException(400, "Could not decode camera frame.")
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(413, "Camera frame too large.")
    prompt = """
Analyze this single captured frame from a user-initiated live camera inspection of a NON-FLIGHT-CRITICAL mock component.
Return JSON: overall_observation; possible_issues (issue, visual_evidence, severity LOW/MEDIUM/HIGH/UNCERTAIN, confidence LOW/MEDIUM/HIGH, location_description);
recommended_next_steps; image_quality (adequate_for_review, limitations); human_review_required true.
Only report visible evidence; no exact coordinates or certainty. A single frame is not continuous automated monitoring. Qualified human review is mandatory.
"""
    try:
        report, model = call_gemini_image(picture, prompt)
    except Exception as exc:
        raise HTTPException(502, f"Vertex AI frame analysis failed: {type(exc).__name__}. Check configuration.")
    event = {"source": "camera_capture", "model": model, "report": report}
    bq_status = log_bigquery("camera_frame_inspection", event)
    pub_status = publish_pubsub("camera_frame_inspection", {"issues": len(report.get("possible_issues", []))})
    return {"inspection_id": str(uuid.uuid4())[:8].upper(), "source": "camera_capture", "model": model,
            "report": report, "analytics_status": bq_status, "event_status": pub_status,
            "safety_note": "User-initiated snapshot analysis, not continuous real-time defect detection. Human review required."}

@app.get("/api/forecast/{component_id}")
def forecast(component_id: str):
    inventory = {r["component_id"]: r for r in load_csv("inventory.csv")}
    demand = [r for r in load_csv("demand_history.csv") if r["component_id"] == component_id]
    if component_id not in inventory:
        raise HTTPException(404, "Unknown component ID.")
    if not demand:
        raise HTTPException(422, "No dated demand history.")
    demand.sort(key=lambda r: r["date"])
    recent = demand[-28:]
    avg = sum(int(r["quantity"]) for r in recent) / len(recent)
    item = inventory[component_id]
    daily = [{"date": (date.today()+timedelta(days=i)).isoformat(), "forecast_units": round(avg)} for i in range(1,8)]
    total = sum(x["forecast_units"] for x in daily)
    incoming = int(item["incoming_quantity"]) if item["incoming_date"] and date.fromisoformat(item["incoming_date"]) <= date.today()+timedelta(days=7) else 0
    available = int(item["current_stock"]) + incoming
    ending = available-total
    safety = int(item["safety_stock"])
    reorder = max(0, total+safety-available)
    risk = "CRITICAL" if ending < 0 else "HIGH" if ending < safety else "MEDIUM" if ending < safety*1.5 else "LOW"
    report = {"component_id": component_id, "component_name": item["component_name"],
        "as_of_date": date.today().isoformat(), "forecast_method": f"Recent-average baseline using {len(recent)} dated records",
        "history_records": len(demand), "history_start": demand[0]["date"], "history_end": demand[-1]["date"],
        "daily_average_units": round(avg,2), "daily_forecast": daily, "forecast_demand_units": total,
        "current_stock_units": int(item["current_stock"]), "incoming_units_in_horizon": incoming,
        "available_units": available, "safety_stock_units": safety, "projected_stock_after_demand": ending,
        "shortage_vs_safety_stock_units": max(0,safety-ending), "suggested_reorder_units": reorder,
        "estimated_reorder_cost": round(reorder*float(item["unit_cost"]),2), "lead_time_days": int(item["lead_time_days"]),
        "risk": risk, "assumptions": ["Recent average is a simple baseline, not a production-grade time-series model.",
        "Incoming receipts count only when scheduled within the seven-day horizon.",
        "Supplier variability, production schedules and cancellations are not modeled."]}
    report["analytics_status"] = log_bigquery("supply_forecast", report)
    report["event_status"] = publish_pubsub("supply_forecast", {"component_id": component_id, "risk": risk, "shortage": report["shortage_vs_safety_stock_units"]})
    return report


class SensorReading(BaseModel):
    machine_id: str = Field(default="MACHINE-01", max_length=40)
    temperature_c: float = Field(ge=0, le=250)
    torque_nm: float = Field(ge=0, le=1000)
    rpm: float = Field(ge=0, le=20000)
    tool_wear_min: float = Field(ge=0, le=10000)

@app.post("/api/maintenance/predict")
def maintenance_predict(reading: SensorReading):
    reasons, score = [], 5
    if reading.temperature_c >= 80:
        score += 35
        reasons.append("High operating temperature")
    elif reading.temperature_c >= 65:
        score += 15
        reasons.append("Elevated operating temperature")
    if reading.torque_nm >= 65:
        score += 25
        reasons.append("High torque")
    elif reading.torque_nm >= 50:
        score += 10
        reasons.append("Elevated torque")
    if reading.tool_wear_min >= 180:
        score += 30
        reasons.append("High tool wear")
    elif reading.tool_wear_min >= 120:
        score += 15
        reasons.append("Elevated tool wear")
    if reading.rpm < 500 or reading.rpm > 4000:
        score += 10
        reasons.append("RPM outside demo operating range")
    # Add a transparent historical-baseline anomaly score using the bundled sensor CSV.
    # This is an unsupervised deviation indicator, not a failure probability.
    historical = [r for r in load_csv("sensor_readings.csv") if r.get("event_level") != "HIGH"]
    current_values = {"temperature_c": reading.temperature_c, "torque_nm": reading.torque_nm,
                      "rpm": reading.rpm, "tool_wear_min": reading.tool_wear_min}
    baseline = {}
    deviations = []
    for field, current in current_values.items():
        vals = [float(r[field]) for r in historical if r.get(field) not in (None, "")]
        mean = sum(vals) / max(1, len(vals))
        variance = sum((v-mean)**2 for v in vals) / max(1, len(vals))
        std = variance ** 0.5
        z = (current-mean)/std if std > 0 else 0.0
        baseline[field] = {"mean": round(mean, 2), "std_dev": round(std, 2), "current": round(current, 2), "z_score": round(z, 2)}
        if abs(z) >= 2.5:
            deviations.append(f"{field} is {abs(z):.1f} standard deviations from the historical normal-event baseline")
    anomaly_score = min(100, round(max([abs(x["z_score"]) for x in baseline.values()] or [0]) * 20))
    if deviations:
        reasons.extend(deviations)
        score = max(score, min(99, anomaly_score))
    score = min(score, 99)
    risk = "HIGH" if score >= 60 else ("MEDIUM" if score >= 30 else "LOW")
    result = {
        "machine_id": reading.machine_id,
        "risk_score": score,
        "risk_level": risk,
        "historical_anomaly_score": anomaly_score,
        "historical_baseline": baseline,
        "historical_anomalies": deviations,
        "observations": reasons or ["No configured demo threshold or large historical deviation was detected"],
        "recommended_checks": [
            "Review sensor trend",
            "Verify readings against approved equipment limits",
            "Check maintenance history"
        ],
        "model_note": "Hybrid demo: configured thresholds plus historical z-score deviation from normal-event rows. Not a validated failure prediction model; synthetic baseline only."
    }
    result["analytics_status"] = log_bigquery("maintenance_assessment", result)
    result["event_status"] = publish_pubsub("maintenance_assessment", {
        "machine_id": reading.machine_id, "risk_level": risk, "risk_score": score
    })
    return result

@app.get("/api/operations-insights")
def operations_insights():
    inventory = load_csv("inventory.csv")
    demand = load_csv("demand_history.csv")
    sensors = load_csv("sensor_readings.csv")
    safety = load_csv("safety_observations.csv")
    waste = load_csv("waste_log.csv")
    insight_list = []
    for item in inventory:
        comp = item["component_id"]
        hist = [int(r["quantity"]) for r in demand if r["component_id"] == comp]
        avg = sum(hist[-14:]) / max(1,len(hist[-14:]))
        forecast7 = round(avg*7)
        avail = int(item["current_stock"])+int(item["incoming_quantity"])
        gap = forecast7 + int(item["safety_stock"]) - avail
        if gap > 0:
            insight_list.append({"category":"SUPPLY", "priority":"HIGH", "title":f"{item['component_name']} may fall below safety stock",
                "evidence":f"7-day baseline demand {forecast7} units vs stock plus listed receipts {avail} units; safety stock {item['safety_stock']}.",
                "recommendation":f"Review supplier schedule and consider a draft reorder of {gap} units."})
    high = [x for x in sensors if x["event_level"]=="HIGH"]
    if high:
        insight_list.append({"category":"MAINTENANCE","priority":"HIGH","title":f"{len(high)} high-level sensor events in demo history",
            "evidence":"Synthetic sensor log contains high-level temperature/torque/tool-wear scenarios.",
            "recommendation":"Review the simulated machine, validate readings and route a maintenance inspection for qualified review."})
    open_items=[x for x in safety if x["status"]!="CLOSED"]
    if open_items:
        insight_list.append({"category":"SAFETY","priority":"MEDIUM","title":f"{len(open_items)} workplace safety observations remain open",
            "evidence":"Synthetic observations include housekeeping, blocked access and ergonomics examples.",
            "recommendation":"Prioritize hazards by severity, assign an owner and verify closure after corrective action."})
    total_waste=sum(float(x["waste_kg"]) for x in waste)
    insight_list.append({"category":"SUSTAINABILITY","priority":"MEDIUM","title":f"{total_waste:.1f} kg recorded in demo waste log",
        "evidence":"Synthetic scrap and packaging records across the sample period.",
        "recommendation":"Classify the largest waste streams, track scrap per unit produced and test a reduction action before claiming savings."})
    result={"generated_at":datetime.now(timezone.utc).isoformat(),"insights":insight_list,
            "note":"Rules-based synthesis of synthetic operational data. Recommendations are advisory and should be reviewed by operations owners."}
    result["analytics_status"]=log_bigquery("operations_insights", result)
    return result

class WorkerCapacity(BaseModel):
    team_size: int = Field(ge=1, le=10000)
    shift_hours: float = Field(gt=0, le=24)
    planned_absence_percent: float = Field(ge=0, le=100)
    task_hours_per_unit: float = Field(gt=0, le=1000)
    safety_allowance_percent: float = Field(default=15, ge=0, le=80)

@app.post("/api/workforce/capacity")
def workforce_capacity(req: WorkerCapacity):
    available_hours = req.team_size * req.shift_hours * (1 - req.planned_absence_percent/100)
    productive_hours = available_hours * (1 - req.safety_allowance_percent/100)
    capacity = int(productive_hours / req.task_hours_per_unit)
    return {"team_size": req.team_size, "shift_hours": req.shift_hours,
        "estimated_available_hours": round(available_hours,2),
        "planning_hours_after_allowance": round(productive_hours,2),
        "estimated_units_capacity": capacity,
        "assumptions": ["Planning estimate only; not an individual worker productivity score.",
        "Includes explicit absence and safety/rest allowance assumptions.",
        "Does not use personal or biometric data and must not be used to rank employees."]}


class CopilotRequest(BaseModel):
    question: str = Field(min_length=5, max_length=1200)

@app.post("/api/copilot")
def operations_copilot(req: CopilotRequest):
    """Grounded cross-functional recommendation. Gemini is used when configured; fallback remains transparent."""
    inventory = load_csv("inventory.csv")
    demand = load_csv("demand_history.csv")
    sensors = load_csv("sensor_readings.csv")
    safety = load_csv("safety_observations.csv")
    waste = load_csv("waste_log.csv")
    evidence = {"inventory": inventory, "demand_history": demand[-80:], "sensor_readings": sensors[-80:],
                "open_safety_observations": [x for x in safety if x.get("status") != "CLOSED"],
                "waste_log": waste[-80:], "question": req.question}
    result = None
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    try:
        from google import genai
        from google.genai import types
        client = get_vertex_client()
        response = client.models.generate_content(
            model=model,
            contents="You are AeroMind, an advisory manufacturing operations copilot. Use ONLY the JSON evidence provided. Rank recommendations by safety/quality first, then production continuity, then cost/waste. Never claim certainty, invent data, or autonomously execute actions. Output JSON with summary (string), prioritized_actions (array of objects with priority, action, evidence, expected_impact, owner_role, approval_required), dependencies (array of strings), uncertainties (array of strings), human_review_required (true). If a signal is not present, say so. Synthetic demo data is not real factory data.\nEVIDENCE:\n" + json.dumps(evidence),
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2))
        result = json.loads(response.text or "{}")
        result["mode"] = "Vertex AI Gemini grounded in bundled demo data"
    except Exception as exc:
        # Deterministic fallback so the UI works before cloud credentials are configured.
        actions = []
        for item in inventory:
            comp = item["component_id"]
            hist = [int(r["quantity"]) for r in demand if r["component_id"] == comp]
            avg = sum(hist[-14:]) / max(1, len(hist[-14:]))
            forecast = round(avg * 7)
            available = int(item["current_stock"]) + int(item["incoming_quantity"])
            gap = forecast + int(item["safety_stock"]) - available
            if gap > 0:
                actions.append({"priority":"HIGH", "action":f"Review reorder of {gap} units for {item['component_name']}", "evidence":f"Baseline 7-day demand {forecast}; stock plus listed receipts {available}; safety stock {item['safety_stock']}.", "expected_impact":"May reduce stockout risk if supplier timing and demand assumptions hold.", "owner_role":"Supply planner", "approval_required":True})
        if any(x.get("event_level") == "HIGH" for x in sensors):
            actions.append({"priority":"HIGH", "action":"Validate high-level sensor events and schedule a qualified machine review", "evidence":"Synthetic sensor history includes high-level events.", "expected_impact":"May help identify abnormal conditions before further production use.", "owner_role":"Maintenance lead", "approval_required":True})
        if any(x.get("status") != "CLOSED" for x in safety):
            actions.append({"priority":"HIGH", "action":"Verify open workplace observations in person and follow site procedure", "evidence":f"{sum(x.get('status') != 'CLOSED' for x in safety)} synthetic observations are open.", "expected_impact":"Supports hazard closure tracking.", "owner_role":"Safety lead", "approval_required":True})
        actions.append({"priority":"MEDIUM", "action":"Measure the largest waste stream per good unit before choosing a reduction experiment", "evidence":f"{sum(float(x['waste_kg']) for x in waste):.1f} kg in synthetic waste log.", "expected_impact":"Establishes a baseline for a measurable waste-reduction trial.", "owner_role":"Production / sustainability lead", "approval_required":True})
        result = {"summary":"Prioritize safety and quality checks, then protect production continuity. This is a deterministic fallback because Gemini could not be reached.", "prioritized_actions":actions, "dependencies":["Confirm the affected component and machine IDs.","Verify stock receipts and sensor values against source systems."], "uncertainties":[f"Gemini unavailable ({type(exc).__name__}); response generated from transparent rules.","All bundled records are synthetic and recommendations require qualified human review."], "human_review_required":True, "mode":"Rules-based fallback; Vertex AI not connected"}
    result["analytics_status"] = log_bigquery("operations_copilot", {"question": req.question, "result": result})
    result["event_status"] = publish_pubsub("operations_copilot_recommendations", {"action_count":len(result.get("prioritized_actions", [])), "mode":result.get("mode")})
    return result

class SimulationRequest(BaseModel):
    machine_outage_hours: float = Field(default=0, ge=0, le=168)
    planned_shift_hours: float = Field(default=8, gt=0, le=24)
    team_size: int = Field(default=12, ge=1, le=10000)
    absence_percent: float = Field(default=8, ge=0, le=100)
    demand_change_percent: float = Field(default=0, ge=-90, le=300)
    supplier_delay_days: int = Field(default=0, ge=0, le=365)
    baseline_units_per_hour: float = Field(default=6, gt=0, le=10000)
    scrap_rate_percent: float = Field(default=3, ge=0, le=100)
    material_kg_per_unit: float = Field(default=2.5, gt=0, le=10000)
    energy_kwh_per_unit: float = Field(default=4, ge=0, le=100000)

@app.post("/api/simulate")
def simulate(req: SimulationRequest):
    planned_hours = req.team_size * req.planned_shift_hours * (1 - req.absence_percent / 100) * 0.85
    machine_factor = max(0.0, 1 - req.machine_outage_hours / req.planned_shift_hours)
    baseline_capacity = int(req.team_size * req.planned_shift_hours * 0.85 * req.baseline_units_per_hour)
    adjusted_capacity = int(planned_hours * req.baseline_units_per_hour * machine_factor)
    baseline_demand = max(1, round(baseline_capacity * 0.9))
    adjusted_demand = max(0, round(baseline_demand * (1 + req.demand_change_percent / 100)))
    output = min(adjusted_capacity, adjusted_demand)
    gap = max(0, adjusted_demand - adjusted_capacity)
    good_units = output * (1 - req.scrap_rate_percent / 100)
    scrap_units = output - good_units
    result = {"baseline_capacity_units":baseline_capacity, "adjusted_capacity_units":adjusted_capacity,
        "baseline_demand_units":baseline_demand, "adjusted_demand_units":adjusted_demand,
        "estimated_output_units":output, "estimated_capacity_gap_units":gap,
        "estimated_scrap_units":round(scrap_units,1), "estimated_material_kg":round(output*req.material_kg_per_unit,1),
        "estimated_energy_kwh":round(output*req.energy_kwh_per_unit,1),
        "supplier_delay_days":req.supplier_delay_days,
        "supplier_risk":"HIGH" if req.supplier_delay_days >= 7 else "MEDIUM" if req.supplier_delay_days >= 3 else "LOW",
        "recommendations":[], "assumptions":["Scenario estimates only; not a digital twin or validated production optimizer.","Capacity uses a simple team-hours × rate baseline and a fixed 15% safety/rest allowance.","Supplier delay is shown as a risk indicator; component-specific bill-of-material constraints are not simulated.","Material, energy and scrap estimates use the inputs supplied; verify against measured data."]}
    if req.machine_outage_hours > 0: result["recommendations"].append("Validate the outage and compare approved maintenance, rescheduling and alternate-capacity options.")
    if gap > 0: result["recommendations"].append(f"Scenario demand exceeds estimated capacity by {gap} units; review due dates and authorized capacity options.")
    if req.supplier_delay_days >= 3: result["recommendations"].append("Check affected part numbers, stock coverage and supplier commitments before changing the schedule.")
    if req.scrap_rate_percent > 3: result["recommendations"].append("Test a quality improvement experiment and measure scrap per good unit against a verified baseline.")
    if not result["recommendations"]: result["recommendations"].append("No major scenario trigger in the supplied inputs; validate assumptions before using this estimate.")
    result["analytics_status"] = log_bigquery("production_simulation", result)
    result["event_status"] = publish_pubsub("production_simulation", {"capacity_gap_units":gap,"supplier_risk":result["supplier_risk"]})
    return result


class ActionRequest(BaseModel):
    action_type: str = Field(pattern="^(INSPECTION|PURCHASE|MAINTENANCE|SAFETY|WASTE_REDUCTION)$")
    summary: str = Field(min_length=3, max_length=300)
    approved_by: str = Field(min_length=2, max_length=80)

@app.post("/api/actions")
def create_action(action: ActionRequest):
    action_id = f"DEMO-{date.today().strftime('%Y%m%d')}-{uuid.uuid4().hex[:5].upper()}"
    result = {"action_id":action_id,"status":"DRAFT_APPROVED","action_type":action.action_type,
        "summary":action.summary,"approved_by":action.approved_by,"created_date":date.today().isoformat(),
        "note":"Demo only. No real purchase order, maintenance ticket or workplace instruction was sent."}
    result["analytics_status"] = log_bigquery("action_approval", result)
    result["event_status"] = publish_pubsub("action_approval", result)
    return result
