from __future__ import annotations
import base64, json, logging, os, random, re, socket, ssl, time, urllib.parse, urllib.request
from typing import Literal
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from urllib.error import HTTPError, URLError
if __package__:  # `uvicorn backend.main:app` from project root.
    from .config import gemini_api_key, gemini_attempt_timeout_seconds, gemini_model, gemini_timeout_seconds
else:  # `uvicorn main:app` from backend/.
    from config import gemini_api_key, gemini_attempt_timeout_seconds, gemini_model, gemini_timeout_seconds

app = FastAPI(title="Multimodal Construction Intelligence Platform", version="1.0.0")
logger = logging.getLogger("uvicorn.error")

DEFAULT_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
    "https://construct-iq-eight.vercel.app",
]
env_origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "").split(",") if x.strip()]
origins = list(dict.fromkeys(DEFAULT_ALLOWED_ORIGINS + env_origins))
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class PPEItem(StrictModel):
    item: str
    status: Literal["observed", "not_observed", "not_assessable"]
    explanation: str

class Finding(StrictModel):
    title: str
    detail: str
    severity: Literal["low", "medium", "high"]
    confidence: float = Field(ge=0, le=1)
    action: str
    location: str | None = None
    risk_explanation: str | None = None

class ImageAnalysis(StrictModel):
    mode: Literal["safety", "crack"]
    provider: str
    summary: str
    findings: list[Finding]
    limitations: list[str]
    safety_score: int | None = None
    ppe_checklist: list[PPEItem] | None = None
    prioritized_actions: list[str] | None = None

# Keep Gemini output aligned with the Pydantic response contract.
GEMINI_IMAGE_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "action": {"type": "string"},
                    "location": {"type": "string"},
                    "risk_explanation": {"type": "string"},
                },
                "required": ["title", "detail", "severity", "confidence", "action"],
                "additionalProperties": False,
            },
        },
        "ppe_evaluations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "string"},
                    "status": {"type": "string", "enum": ["observed", "not_observed", "not_assessable"]},
                    "explanation": {"type": "string"},
                },
                "required": ["item", "status", "explanation"],
                "additionalProperties": False,
            },
        },
        "limitations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "findings", "limitations"],
    "additionalProperties": False,
}
class Material(StrictModel):
    name: str
    quantity: float = Field(ge=0, le=100000000)
    unit: str
    rate: float = Field(ge=0, le=1000000)
class PlanRequest(StrictModel):
    area_sqft: float = Field(gt=0, le=10000000)
    location: Literal["Tier 1", "Tier 2", "Tier 3"] = "Tier 1"
    building_type: Literal["Residential", "Commercial", "Industrial", "Residential house", "Apartment building", "Commercial building", "Warehouse / industrial", "Renovation"] = "Residential house"
    property_tier: Literal["Value", "Standard", "Premium", "Luxury"] = "Standard"
    completion: float = Field(default=0, ge=0, le=100)
    # Accepted for compatibility with older clients; it does not affect estimates.
    budget: float | None = Field(default=None, ge=0, le=1000000000000000)
    materials: list[Material] = Field(default_factory=list)
class PlanResponse(StrictModel):
    provider: str
    completion: float
    estimated_total_inr: float
    cost_breakdown: dict[str, float]
    materials: list[Material]
    assumptions: list[str]
    tasks: list[dict[str, str]]
    summary: str
class SiteContext(StrictModel):
    activity: str | None = None
    labour_count: int | None = Field(default=None, ge=0, le=10000)
    site_condition: str | None = None
    location: str | None = None
    weather_summary: str | None = None
    work_timing: str | None = None
    notes: str | None = None

class AssistantRequest(StrictModel):
    message: str = Field(min_length=1, max_length=4000)
    kind: Literal["chat", "daily_report", "context_query"] = "chat"
    site_context: SiteContext | None = None

class AssistantResponse(StrictModel):
    provider: str
    answer: str
    guidance: list[str]
    context_summary: str | None = None
class WeatherCurrent(StrictModel):
    observed_at: str
    temperature_c: float
    precipitation_mm: float
    wind_speed_kmh: float
    weather_code: int
    condition: str

class WeatherForecastDay(StrictModel):
    date: str
    minimum_c: float
    maximum_c: float
    precipitation_probability_percent: float
    weather_code: int
    condition: str

class WeatherResponse(StrictModel):
    available: bool
    location: str
    summary: str
    guidance: list[str]
    current: WeatherCurrent | None = None
    forecast: list[WeatherForecastDay] = Field(default_factory=list)

@app.get("/")
def root(): return {"message": "Construction Intelligence API is running", "status": "ok"}
@app.get("/api/health")
def health(): return {"status": "healthy", "ai_configured": bool(gemini_api_key())}

def _sanitize_log_text(value: str, api_key: str, image_bytes: bytes = b"") -> str:
    """Redact credentials, URLs, and long encoded payloads before logging."""
    if api_key:
        value = value.replace(api_key, "[REDACTED]")
    if image_bytes:
        value = value.replace(base64.b64encode(image_bytes).decode("ascii"), "[IMAGE REDACTED]")
        try:
            value = value.replace(image_bytes.decode("utf-8"), "[IMAGE REDACTED]")
        except UnicodeDecodeError:
            pass
    value = re.sub(r"(?i)(x-goog-api-key|authorization|api[_-]?key)\s*[:=]\s*[^,\s\"']+", r"\1=[REDACTED]", value)
    value = re.sub(r"(?i)bearer\s+[^,\s\"']+", "Bearer [REDACTED]", value)
    value = re.sub(r"https?://\S+", "[URL REDACTED]", value)
    value = re.sub(r"(?<![A-Za-z0-9])[A-Za-z0-9+/=_-]{100,}(?![A-Za-z0-9])", "[PAYLOAD REDACTED]", value)
    return value[:1600]

def _sanitize_provider_value(value, api_key: str, field: str = "", image_bytes: bytes = b""):
    """Sanitize provider JSON recursively; never log echoed request/image fields."""
    normalized = re.sub(r"[^a-z]", "", field.lower())
    if normalized in {"data", "inlinedata", "image", "contents", "parts", "authorization", "xgoogapikey", "apikey"}:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {k: _sanitize_provider_value(v, api_key, str(k), image_bytes) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_provider_value(item, api_key, field, image_bytes) for item in value[:30]]
    if isinstance(value, str):
        return _sanitize_log_text(value, api_key, image_bytes)
    return value

def _sanitized_provider_error(body: bytes, api_key: str, image_bytes: bytes = b"") -> str:
    """Return a safe, bounded representation of the Gemini error response."""
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
        safe_payload = _sanitize_provider_value(payload, api_key, image_bytes=image_bytes)
        return json.dumps(safe_payload, ensure_ascii=True)[:1600]
    except (ValueError, AttributeError):
        return _sanitize_log_text(body.decode("utf-8", errors="replace"), api_key, image_bytes) or "[empty response body]"

def _log_provider_exception(
    exc: Exception,
    api_key: str,
    image_bytes: bytes = b"",
    stage: str = "analysis",
    model: str | None = None,
    provider_status: int | None = None,
) -> None:
    """Log a useful exception category and a conservative safe message."""
    model_name = model or gemini_model()
    is_validation = (
        isinstance(exc, ValidationError)
        or type(exc).__name__ == "ValidationError"
        or "ValidationError" in type(exc).__name__
    )
    if isinstance(exc, json.JSONDecodeError):
        message = f"invalid JSON at line {exc.lineno}, column {exc.colno}"
    elif is_validation:
        errors = []
        if hasattr(exc, "errors") and callable(exc.errors):
            try:
                errors = exc.errors(include_input=False, include_context=False)
            except TypeError:
                try:
                    errors = exc.errors()
                except Exception:
                    errors = []
            except Exception:
                errors = []
        safe_fields = []
        for error in errors[:8]:
            if isinstance(error, dict):
                location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
                error_type = str(error.get("type", "validation_error"))
                safe_fields.append(_sanitize_log_text(f"{location}:{error_type}", api_key, image_bytes))
        message = "response schema validation failed" + (f" ({'; '.join(safe_fields)})" if safe_fields else "")
    elif isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, (TimeoutError, socket.timeout)):
        message = f"Gemini request timed out after {gemini_timeout_seconds():g} seconds"
    elif isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        message = _sanitize_log_text(str(reason), api_key, image_bytes) if isinstance(reason, (str, OSError)) else "network request failed"
    elif isinstance(exc, (TimeoutError, socket.timeout)):
        message = f"Gemini request timed out after {gemini_timeout_seconds():g} seconds"
    elif isinstance(exc, KeyError):
        field = exc.args[0] if exc.args else "unknown"
        message = f"missing provider response field: {field}" if field in {"candidates", "content", "parts", "text", "summary", "findings", "limitations"} else "missing provider response field"
    elif isinstance(exc, (ValueError, IndexError, TypeError)):
        exc_str = str(exc)
        if "blocked" in exc_str:
            message = "provider blocked content"
        elif "did not contain answer text" in exc_str:
            message = "missing provider answer text"
        elif "missing candidates" in exc_str:
            message = "missing provider candidates"
        elif "missing content" in exc_str:
            message = "missing provider candidate content"
        elif "missing parts" in exc_str:
            message = "missing provider candidate parts"
        elif "not a JSON object" in exc_str:
            message = "provider response is not a JSON object"
        else:
            message = "invalid provider response structure"
    else:
        message = "details omitted to avoid logging request data"
    if provider_status is not None:
        logger.warning(
            "Gemini analysis failed: exception_type=%s, stage=%s, model=%s, provider_status=%s, safe_message=%s",
            type(exc).__name__, stage, model_name, provider_status, message,
        )
    else:
        logger.warning(
            "Gemini analysis failed: exception_type=%s, stage=%s, model=%s, safe_message=%s",
            type(exc).__name__, stage, model_name, message,
        )

def _is_timeout_error(exc: Exception) -> bool:
    return isinstance(exc, (TimeoutError, socket.timeout)) or (
        isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, (TimeoutError, socket.timeout))
    )

def _gemini_model_request(model: str, payload: dict, key: str, image: bytes, deadline: float) -> bytes:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(model)}:generateContent"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "x-goog-api-key": key}, method="POST")
    for retry_count in range(3):  # initial attempt plus at most two retries
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning("Gemini attempt not started: model=%s attempt=%s timeout_seconds=0 reason=budget exhausted", model, retry_count + 1)
            raise TimeoutError("Gemini overall timeout budget exceeded")
        attempt_timeout = min(gemini_attempt_timeout_seconds(), remaining)
        try:
            with urllib.request.urlopen(req, timeout=attempt_timeout) as resp:
                return resp.read()
        except HTTPError as exc:
            safe_body = _sanitized_provider_error(exc.read(8192), key, image)
            logger.warning(
                "Gemini API HTTP error: model=%s attempt=%s timeout_seconds=%.2f status=%s retry_count=%s sanitized_response_body=%s",
                model, retry_count + 1, attempt_timeout, exc.code, retry_count, safe_body,
            )
            if exc.code not in {500, 503} or retry_count >= 2:
                raise
            delay = random.uniform(0.0, min(0.5 * (2 ** retry_count), 8.0))
            if time.monotonic() + delay >= deadline:
                raise TimeoutError("Gemini overall timeout budget exhausted during retry backoff") from exc
            time.sleep(delay)
        except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
            if _is_timeout_error(exc):
                logger.warning(
                    "Gemini attempt timed out: model=%s attempt=%s timeout_seconds=%.2f reason=provider socket timeout",
                    model, retry_count + 1, attempt_timeout,
                )
            else:
                _log_provider_exception(exc, key, image, stage="network_request", model=model)
                logger.warning(
                    "Gemini attempt failed: model=%s attempt=%s timeout_seconds=%.2f reason=network error",
                    model, retry_count + 1, attempt_timeout,
                )
            raise
    raise RuntimeError("Gemini retry loop ended unexpectedly")

def gemini(prompt: str, image: bytes | None = None, mime: str = "image/jpeg") -> str:
    key = gemini_api_key()
    if not key: raise RuntimeError("Gemini is not configured")
    parts = [{"text": prompt}]
    if image: parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(image).decode()}})
    # Gemini 3.x rejects legacy sampling parameters such as temperature.
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseFormat": {
                "text": {
                    "mimeType": "APPLICATION_JSON",
                    "schema": GEMINI_IMAGE_ANALYSIS_SCHEMA,
                },
            },
        },
    }
    timeout_budget = gemini_timeout_seconds()
    deadline = time.monotonic() + timeout_budget
    model = gemini_model()
    # Use only the configured primary model; bounded retries, when applicable,
    # stay on this same model.
    response = _gemini_model_request(model, payload, key, image or b"", deadline)
    selected_model = model
    try:
        rest_response = json.loads(response)
        if not isinstance(rest_response, dict):
            raise ValueError("Gemini response is not a JSON object")
        candidates = rest_response.get("candidates")
        if not candidates or not isinstance(candidates, list):
            prompt_feedback = rest_response.get("promptFeedback")
            if isinstance(prompt_feedback, dict) and prompt_feedback.get("blockReason"):
                raise ValueError(f"Gemini response blocked: {prompt_feedback.get('blockReason')}")
            raise ValueError("Gemini response missing candidates")
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise ValueError("Gemini candidate is not a JSON object")
        finish_reason = candidate.get("finishReason")
        if finish_reason in {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}:
            raise ValueError(f"Gemini candidate blocked by finishReason: {finish_reason}")
        content = candidate.get("content")
        if not content or not isinstance(content, dict):
            raise ValueError("Gemini candidate missing content")
        parts = content.get("parts")
        if not parts or not isinstance(parts, list):
            raise ValueError("Gemini candidate content missing parts")
        answer_text = "".join(
            part["text"]
            for part in parts
            if isinstance(part, dict)
            and part.get("thought") is not True
            and isinstance(part.get("text"), str)
        )
        if not answer_text:
            raise ValueError("Gemini response did not contain answer text")
        cleaned_text = answer_text.strip()
        if cleaned_text.startswith("```"):
            cleaned_text = re.sub(r"^```(?:json)?\s*", "", cleaned_text, flags=re.IGNORECASE)
            cleaned_text = re.sub(r"\s*```$", "", cleaned_text)
        return cleaned_text
    except Exception as exc:
        _log_provider_exception(exc, key, image or b"", stage="gemini_extraction", model=selected_model)
        logger.warning("Gemini response parsing failed: model=%s", selected_model)
        raise

def compute_safety_score(findings: list[Finding], ppe_items: list[PPEItem]) -> int | None:
    """
    Documented, deterministic scoring method based on identified hazards, severity, and PPE evidence:
    - Base score: 100 points represents an ideal site observation.
    - High severity hazard: -18 points (scaled by confidence, min 12 points).
    - Medium severity hazard: -10 points (scaled by confidence, min 6 points).
    - Low severity hazard: -4 points (scaled by confidence, min 2 points).
    - PPE checklist: -10 points for each essential category marked 'not_observed'.
    - Safety ceiling: score capped at 65 if any high-severity hazard exists, max 40 if 3+ high hazards.
    - Normalized strictly to the range 5..100.
    - Returns None only if no assessable findings or PPE data exist.
    """
    if not findings and not ppe_items:
        return None
    score = 100.0
    high_count = 0
    for f in findings:
        conf = max(0.5, min(1.0, float(f.confidence if f.confidence is not None else 0.8)))
        if f.severity == "high":
            high_count += 1
            score -= max(12.0, 18.0 * conf)
        elif f.severity == "medium":
            score -= max(6.0, 10.0 * conf)
        elif f.severity == "low":
            score -= max(2.0, 4.0 * conf)

    # High severity hazards cap the site baseline
    if high_count >= 3:
        score = min(score, 40.0)
    elif high_count >= 1:
        score = min(score, 65.0)

    for ppe in ppe_items:
        if ppe.status == "not_observed":
            score -= 10.0

    return max(5, min(100, round(score)))

def build_ppe_checklist(raw_evaluations: list | None, findings: list[Finding]) -> list[PPEItem]:
    """
    Build evidence-based PPE checklist for the 4 core categories:
    - Hard hats
    - High-visibility vests
    - Safety footwear
    - Fall-arrest/restraint equipment
    """
    categories = [
        ("Hard hats", ["hard hat", "helmet", "head protection"]),
        ("High-visibility vests", ["hi-vis", "vest", "reflective", "visibility"]),
        ("Safety footwear", ["boot", "footwear", "shoe"]),
        ("Fall-arrest/restraint equipment", ["fall", "harness", "lanyard", "scaffold", "guardrail", "edge", "height"]),
    ]

    provided_map = {}
    if isinstance(raw_evaluations, list):
        for raw in raw_evaluations:
            if isinstance(raw, dict) and "item" in raw and "status" in raw:
                item_name = str(raw.get("item", "")).strip().lower()
                status = str(raw.get("status", "")).strip().lower()
                if status in {"observed", "not_observed", "not_assessable"}:
                    explanation = str(raw.get("explanation", "")).strip() or "Evaluated from visible site photograph."
                    provided_map[item_name] = (status, explanation)

    findings_text = " ".join(f"{f.title} {f.detail} {f.action}".lower() for f in findings)

    checklist: list[PPEItem] = []
    for cat_name, keywords in categories:
        cat_lower = cat_name.lower()
        matched = False
        for p_name, (st, exp) in provided_map.items():
            if any(k in p_name for k in keywords) or p_name in cat_lower or cat_lower in p_name:
                checklist.append(PPEItem(item=cat_name, status=st, explanation=exp))
                matched = True
                break
        if not matched:
            has_defect = any(k in findings_text for k in keywords)
            if has_defect and any(word in findings_text for word in ["missing", "lack", "without", "unprotected", "no "]):
                checklist.append(PPEItem(
                    item=cat_name,
                    status="not_observed",
                    explanation=f"Visible deficiency or risk related to {cat_name.lower()} indicated in observed site hazards.",
                ))
            else:
                checklist.append(PPEItem(
                    item=cat_name,
                    status="not_assessable",
                    explanation="Not assessable or workers/equipment not clearly discernable from this camera perspective.",
                ))

    return checklist

def build_prioritized_actions(findings: list[Finding]) -> list[str]:
    """Distinguish urgent immediate precautions from routine improvements."""
    urgent = []
    routine = []
    for f in findings:
        act = f.action.strip()
        if not act:
            continue
        if f.severity == "high":
            urgent.append(f"Immediate: {act}")
        elif f.severity == "medium":
            urgent.append(f"Priority: {act}")
        else:
            routine.append(f"Routine: {act}")
    actions = urgent + routine
    if not actions:
        actions.append("Conduct scheduled periodic safety inspection with qualified site supervisor.")
    actions.append("Observations are visual only; verify compliance with the responsible safety professional.")
    return actions

@app.post("/api/analyze", response_model=ImageAnalysis)
async def analyze(mode: Literal["safety", "crack"], image: UploadFile = File(...)):
    data = await image.read()
    if not image.content_type or not image.content_type.startswith("image/"): raise HTTPException(415, "Upload an image file")
    if len(data) > 12 * 1024 * 1024: raise HTTPException(413, "Image must be 12 MB or smaller")
    instruction = (
        "You are an expert construction site safety auditor reviewing this site photograph. "
        "Evaluate visible evidence only; do not infer hidden facts. "
        "Return JSON with: "
        "1. summary: concise construction-specific summary of observed site conditions and main visible risks. "
        "2. findings: array of visible hazards, each with title, detail (visible evidence and location), "
        "severity (low|medium|high), confidence (0..1), action (recommended corrective action), "
        "location (visible location on site), and risk_explanation (potential risk / consequence). "
        "3. ppe_evaluations: array for the 4 core PPE items (Hard hats, High-visibility vests, Safety footwear, Fall-arrest/restraint equipment), "
        "each with item, status ('observed', 'not_observed', or 'not_assessable'), and explanation "
        "(if workers are not visible, use 'not_assessable' with clear explanation; do not label noncompliant if workers are absent). "
        "4. limitations: array of visual observation caveats."
        if mode == "safety" else
        "Describe the visible crack pattern, cautious severity and possible causes, and inspection/remediation steps. "
        "Never infer precise dimensions without scale. Return JSON summary, findings array(title,detail,severity low|medium|high,confidence 0..1,action),limitations array."
    )
    try:
        raw_text = gemini(instruction, data, image.content_type)
        parsed = json.loads(raw_text)
        if not isinstance(parsed, dict):
            raise ValueError("Gemini response is not a JSON object")
        raw_limitations = parsed.get("limitations")
        limitations = [str(x) for x in raw_limitations] if isinstance(raw_limitations, list) else []

        if mode == "safety":
            limitations.append("Visual observations only; PPE compliance is not verified.")
        else:
            limitations.append("No precise crack dimensions can be inferred without a scale reference.")

        raw_findings = parsed.get("findings")

        # Validate findings structure through ImageAnalysis
        initial_analysis = ImageAnalysis(
            mode=mode,
            provider="Gemini",
            summary=parsed.get("summary"),
            findings=raw_findings,
            limitations=limitations,
        )

        if mode == "safety":
            severity_order = {"high": 0, "medium": 1, "low": 2}
            sorted_findings = sorted(initial_analysis.findings, key=lambda f: severity_order.get(f.severity, 99))
            ppe_checklist = build_ppe_checklist(parsed.get("ppe_evaluations"), sorted_findings)
            safety_score = compute_safety_score(sorted_findings, ppe_checklist)
            prioritized_actions = build_prioritized_actions(sorted_findings)
            return ImageAnalysis(
                mode=mode,
                provider="Gemini",
                summary=parsed["summary"],
                findings=sorted_findings,
                limitations=limitations,
                safety_score=safety_score,
                ppe_checklist=ppe_checklist,
                prioritized_actions=prioritized_actions,
            )
        else:
            return initial_analysis
    except RuntimeError:
        raise HTTPException(503, "AI analysis unavailable: configure GEMINI_API_KEY. No demo result was generated.")
    except Exception as e:
        # Provider exceptions can include the request URL, which contains the
        # API key as a query parameter. Never return raw provider error text.
        if not isinstance(e, HTTPError) and not _is_timeout_error(e):
            status = getattr(e, "code", None) or getattr(e, "status_code", None)
            _log_provider_exception(
                e,
                gemini_api_key() or "",
                data,
                stage="response_model",
                model=gemini_model(),
                provider_status=status if isinstance(status, int) else None,
            )
        raise HTTPException(502, "AI provider error or invalid response")

@app.post("/api/plan", response_model=PlanResponse)
def plan(req: PlanRequest):
    # Indicative base benchmarks per sq ft; selected tier factors are stated in the output.
    base_rates = {
        "Residential": 1900, "Residential house": 1900, "Apartment building": 2200,
        "Commercial": 2500, "Commercial building": 2500,
        "Industrial": 1700, "Warehouse / industrial": 1700, "Renovation": 1500,
    }
    city_factors = {"Tier 1": 1.18, "Tier 2": 1.08, "Tier 3": 1.0}
    property_factors = {"Value": 0.9, "Standard": 1.0, "Premium": 1.2, "Luxury": 1.45}
    materials = req.materials or [
        Material(name="Cement", quantity=round(req.area_sqft * .42, 1), unit="bags", rate=390),
        Material(name="Steel", quantity=round(req.area_sqft * 3.8, 1), unit="kg", rate=68),
        Material(name="Sand", quantity=round(req.area_sqft * 1.2, 1), unit="cu ft", rate=55),
    ]
    material_cost = sum(m.quantity * m.rate for m in materials)
    benchmark = max(base_rates[req.building_type] * req.area_sqft, material_cost)
    total = benchmark * city_factors[req.location] * property_factors[req.property_tier]
    breakdown = {"Materials": round(total * .52), "Labour": round(total * .24), "Services": round(total * .12), "Finishes": round(total * .08), "Contingency": round(total * .04)}
    assumptions = [
        f"Base indicative rate for {req.building_type}: ₹{base_rates[req.building_type]:,.0f}/sq ft before tier factors.",
        f"City {req.location} factor: {city_factors[req.location]:.2f}×; this is a broad location allowance, not a city-specific quote.",
        f"Property {req.property_tier} factor: {property_factors[req.property_tier]:.2f}×; this represents an indicative specification/finish allowance.",
        "Material benchmark quantities and rates are approximate; review them in the editable material inputs and validate locally.",
        "The task sequence and durations are a basic planning template; confirm the schedule against actual site conditions and resources.",
        "Estimate excludes site-specific design, taxes, approvals, access constraints and market changes; it is not a professional valuation, tender or quotation.",
    ]
    return PlanResponse(
        provider="Deterministic estimate", completion=req.completion, estimated_total_inr=round(total),
        cost_breakdown=breakdown, materials=materials, assumptions=assumptions,
        tasks=[
            {"task": "Site setup & foundations", "duration": "Weeks 1–4", "resource": "Civil crew, excavator"},
            {"task": "Structure & slab", "duration": "Weeks 5–12", "resource": "Formwork, steel, concrete crew"},
            {"task": "MEP rough-in & masonry", "duration": "Weeks 13–18", "resource": "Electrical, plumbing, masonry teams"},
            {"task": "Finishes & handover", "duration": "Weeks 19–24", "resource": "Finishing crew, QA"},
        ],
        summary=f"Indicative benchmark estimate for {req.area_sqft:,.0f} sq ft; confirm specifications, quantities and local rates before commitment.",
    )

@app.post("/api/assistant", response_model=AssistantResponse)
def assistant(req: AssistantRequest):
    context_lines = []
    if req.site_context:
        ctx = req.site_context
        if ctx.activity: context_lines.append(f"Planned Activity: {ctx.activity}")
        if ctx.labour_count is not None: context_lines.append(f"Workforce Count: {ctx.labour_count} workers on site")
        if ctx.site_condition: context_lines.append(f"Ground & Site Conditions: {ctx.site_condition}")
        if ctx.location: context_lines.append(f"Site Location: {ctx.location}")
        if ctx.weather_summary: context_lines.append(f"Weather Context: {ctx.weather_summary}")
        if ctx.work_timing: context_lines.append(f"Work Shift / Timing: {ctx.work_timing}")
        if ctx.notes: context_lines.append(f"Site Notes & Constraints: {ctx.notes}")

    context_summary = " · ".join(context_lines) if context_lines else None

    if gemini_api_key():
        try:
            if req.kind == "daily_report":
                prompt_prefix = "Prepare a concise, professional daily construction site progress and safety report using the provided site context and notes; clearly flag any missing details:\n"
            else:
                prompt_prefix = (
                    "You are ConstructIQ's Senior Field Construction Assistant. "
                    "Provide practical, context-aware engineering and safety guidance for the site query below. "
                    "Directly address whether the planned activity is feasible under the reported conditions, "
                    "potential slip/access/weather hazards, workforce coordination, safety/PPE checks, "
                    "and conditions requiring work to pause or seek professional review. "
                    "Structure your answer with clear section titles and actionable bullet points.\n"
                )
            context_block = f"\n[Site Context]\n" + "\n".join(f"- {line}" for line in context_lines) + "\n" if context_lines else ""
            prompt = f"{prompt_prefix}{context_block}\n[User Query / Notes]\n{req.message}"
            answer = gemini(prompt)
            try:
                answer = json.loads(answer).get("answer", answer)
            except Exception:
                pass
            guidance = [
                "Decision support only; verify site conditions with the responsible project engineer.",
                "Observations and guidelines do not replace local statutory building codes or health and safety regulations.",
            ]
            return AssistantResponse(
                provider="Gemini",
                answer=str(answer),
                guidance=guidance,
                context_summary=context_summary,
            )
        except Exception as exc:
            logger.warning("Gemini assistant query failed: %s", type(exc).__name__)

    # Fallback / Offline guidance
    if req.kind == "daily_report":
        guidance_items = [
            "Date, location, and verified weather records",
            "Work completed and measured quantities against milestone schedule",
            f"Crew deployment ({req.site_context.labour_count if req.site_context and req.site_context.labour_count is not None else 'as recorded'}) and equipment operating hours",
            "Deliveries, batch tickets, and quality inspections",
            f"Safety observations, PPE compliance, and site ground state ({req.site_context.site_condition if req.site_context and req.site_context.site_condition else 'unspecified'})",
            "Delays, weather disruptions, and next shift plan",
        ]
        answer_text = (
            f"Daily Site Report Structure ({req.site_context.activity if req.site_context and req.site_context.activity else 'General Construction'})\n"
            f"Location: {req.site_context.location if req.site_context and req.site_context.location else 'Site Records'}\n"
            f"Conditions: {req.site_context.site_condition if req.site_context and req.site_context.site_condition else 'Standard'}\n"
            f"Weather: {req.site_context.weather_summary if req.site_context and req.site_context.weather_summary else 'Refer to live weather advisor'}\n\n"
            "Fill in site metrics from verified daily logs before supervisor sign-off."
        )
        return AssistantResponse(
            provider="Guided template",
            answer=answer_text,
            guidance=guidance_items,
            context_summary=context_summary,
        )

    # Context-aware offline answer
    activity_desc = req.site_context.activity if req.site_context and req.site_context.activity else "General site work"
    condition_desc = req.site_context.site_condition if req.site_context and req.site_context.site_condition else "reported conditions"
    weather_desc = req.site_context.weather_summary if req.site_context and req.site_context.weather_summary else "applicable weather forecast"
    offline_answer = (
        f"Contextual Guidance for {activity_desc}:\n\n"
        f"1. Ground & Environmental Precautions: Review work area stability and access routes under {condition_desc}. Verify {weather_desc} before undertaking exposed or moisture-sensitive tasks.\n"
        f"2. Workforce Safety: Ensure all {req.site_context.labour_count if req.site_context and req.site_context.labour_count is not None else 'assigned'} workers are briefed on task sequencing, slip/trip prevention, and appropriate PPE.\n"
        f"3. Quality & Material Protection: Protect freshly laid materials and sensitive components from weather exposure.\n"
        f"4. Stop-Work Criteria: Halt operations immediately if lightning, extreme rain, high winds, or structural instability occur."
    )
    return AssistantResponse(
        provider="Offline guidance",
        answer=offline_answer,
        guidance=[
            "For critical operations, inspect immediate hazards and consult the site engineer.",
            "Confirm applicable local safety regulations and manufacturer specifications.",
        ],
        context_summary=context_summary,
    )

def _weather_condition(code: int) -> str:
    conditions = {
        0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
        45: "Fog", 48: "Freezing fog", 51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
        61: "Light rain", 63: "Rain", 65: "Heavy rain", 71: "Light snow", 73: "Snow",
        75: "Heavy snow", 80: "Rain showers", 81: "Rain showers", 82: "Heavy rain showers",
        95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with hail",
    }
    return conditions.get(code, "Conditions reported")

def _weather_safe_detail(value: str) -> str:
    value = re.sub(r"https?://\S+", "[URL REDACTED]", value)
    value = re.sub(r"(?i)(api[_-]?key|authorization|token)\s*[:=]\s*[^,\s\"']+", r"\1=[REDACTED]", value)
    return re.sub(r"[\r\n\t]+", " ", value)[:400]

def _weather_ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception as exc:
        logger.warning("Could not load certifi CA bundle, falling back to default SSL context: %s", exc)
        return ssl.create_default_context()

def _weather_json(url: str, stage: str) -> dict:
    ctx = _weather_ssl_context()
    try:
        try:
            with urllib.request.urlopen(url, timeout=8, context=ctx) as response:
                payload = json.loads(response.read())
        except TypeError as exc:
            if "context" in str(exc):
                with urllib.request.urlopen(url, timeout=8) as response:
                    payload = json.loads(response.read())
            else:
                raise
    except HTTPError as exc:
        try:
            body = exc.read(1200).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        logger.warning("Weather provider failure: stage=%s kind=http_error status=%s detail=%s", stage, exc.code, _weather_safe_detail(body or exc.reason))
        raise
    except (TimeoutError, socket.timeout) as exc:
        logger.warning("Weather provider failure: stage=%s kind=timeout timeout_seconds=8", stage)
        raise
    except URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            logger.warning("Weather provider failure: stage=%s kind=timeout timeout_seconds=8", stage)
        else:
            logger.warning("Weather provider failure: stage=%s kind=network_error detail=%s", stage, _weather_safe_detail(str(reason)))
        raise
    except OSError as exc:
        logger.warning("Weather provider failure: stage=%s kind=network_error detail=%s", stage, _weather_safe_detail(str(exc)))
        raise
    except json.JSONDecodeError as exc:
        logger.warning("Weather provider failure: stage=%s kind=malformed_json detail=%s", stage, _weather_safe_detail(str(exc)))
        raise
    except Exception as exc:
        logger.warning("Weather provider failure: stage=%s kind=%s detail=%s", stage, type(exc).__name__, _weather_safe_detail(str(exc)))
        raise
    if not isinstance(payload, dict):
        logger.warning("Weather provider failure: stage=%s kind=malformed_response detail=top-level JSON must be an object", stage)
        raise ValueError("invalid weather provider response")
    return payload

def _weather_http_error(stage: str, exc: Exception) -> HTTPException:
    if isinstance(exc, (TimeoutError, socket.timeout)) or (isinstance(exc, URLError) and isinstance(exc.reason, (TimeoutError, socket.timeout))):
        detail = "Weather location lookup timed out. Try again shortly." if stage == "geocoding" else "Live weather request timed out. Try again shortly."
        return HTTPException(503, detail)
    if stage == "geocoding":
        return HTTPException(503, "Location lookup is unavailable. Try again shortly.")
    return HTTPException(503, "Live weather provider is unavailable. Try again shortly.")

@app.get("/api/weather", response_model=WeatherResponse)
def weather(latitude: float | None = None, longitude: float | None = None, location: str | None = None):
    if (latitude is None) != (longitude is None):
        raise HTTPException(422, "Provide both latitude and longitude, or enter a location.")
    place = location.strip() if location else ""
    if len(place) > 120:
        raise HTTPException(422, "Location must be 120 characters or fewer.")
    if latitude is None and not place:
        return WeatherResponse(available=False, location="", summary="Enter a city or location to request live weather.", guidance=["Weather is not inferred when no location is supplied."])

    if latitude is None:
        try:
            geo_url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode({"name": place, "count": 1, "language": "en", "format": "json"})
            geocoding = _weather_json(geo_url, "geocoding")
            results = geocoding.get("results") or []
            if not results:
                raise HTTPException(404, "Location not found. Check the spelling and try again.")
            match = results[0]
            if not isinstance(match, dict):
                raise ValueError("invalid geocoding result shape")
            latitude = float(match["latitude"])
            longitude = float(match["longitude"])
            place = ", ".join(dict.fromkeys(part for part in (match.get("name"), match.get("admin1"), match.get("country")) if part))
        except HTTPException:
            raise
        except Exception as exc:
            if not isinstance(exc, (HTTPError, URLError, TimeoutError, socket.timeout, json.JSONDecodeError, ValueError, KeyError, TypeError)):
                logger.warning("Weather provider failure: stage=geocoding kind=%s detail=%s", type(exc).__name__, _weather_safe_detail(str(exc)))
            if isinstance(exc, (HTTPError, URLError, TimeoutError, socket.timeout, OSError)):
                raise _weather_http_error("geocoding", exc)
            logger.warning("Weather provider failure: stage=geocoding kind=malformed_response detail=%s", _weather_safe_detail(str(exc)))
            raise HTTPException(502, "Location service returned an invalid response. Try again shortly.")
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise HTTPException(422, "Enter a valid location or coordinate pair.")
    place = place or f"{latitude:.3f}, {longitude:.3f}"
    weather_url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": latitude, "longitude": longitude,
        "current": "temperature_2m,precipitation,wind_speed_10m,weather_code",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "forecast_days": 3, "timezone": "auto",
    })
    try:
        payload = _weather_json(weather_url, "forecast")
        current_data = payload["current"]
        if not isinstance(current_data, dict):
            raise ValueError("invalid current conditions shape")
        current_code = int(current_data["weather_code"])
        current = WeatherCurrent(
            observed_at=str(current_data["time"]), temperature_c=float(current_data["temperature_2m"]),
            precipitation_mm=float(current_data["precipitation"]), wind_speed_kmh=float(current_data["wind_speed_10m"]),
            weather_code=current_code, condition=_weather_condition(current_code),
        )
        daily = payload["daily"]
        if not isinstance(daily, dict) or not isinstance(daily.get("time"), list):
            raise ValueError("invalid forecast shape")
        forecast_count = min(3, len(daily["time"]))
        if forecast_count == 0 or any(not isinstance(daily.get(key), list) or len(daily[key]) < forecast_count for key in ("temperature_2m_min", "temperature_2m_max", "precipitation_probability_max", "weather_code")):
            raise ValueError("incomplete forecast arrays")
        forecast = [
            WeatherForecastDay(
                date=str(daily["time"][index]), minimum_c=float(daily["temperature_2m_min"][index]),
                maximum_c=float(daily["temperature_2m_max"][index]),
                precipitation_probability_percent=float(daily["precipitation_probability_max"][index]),
                weather_code=int(daily["weather_code"][index]), condition=_weather_condition(int(daily["weather_code"][index])),
            )
            for index in range(forecast_count)
        ]
    except Exception as exc:
        if isinstance(exc, (HTTPError, URLError, TimeoutError, socket.timeout, OSError)):
            raise _weather_http_error("forecast", exc)
        logger.warning("Weather provider failure: stage=forecast kind=malformed_response detail=%s", _weather_safe_detail(str(exc)))
        raise HTTPException(502, "Weather provider returned an invalid response. Try again shortly.")
    return WeatherResponse(
        available=True, location=place,
        summary=f"{current.condition} · {current.temperature_c:g} °C · precipitation {current.precipitation_mm:g} mm · wind {current.wind_speed_kmh:g} km/h",
        guidance=["Review site-specific limits for lifting, work at height and other weather-sensitive tasks."],
        current=current, forecast=forecast,
    )
