from __future__ import annotations
import base64, json, logging, os, random, re, socket, time, urllib.parse, urllib.request
from typing import Literal
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from urllib.error import HTTPError
if __package__:  # `uvicorn backend.main:app` from project root.
    from .config import gemini_api_key, gemini_attempt_timeout_seconds, gemini_fallback_model, gemini_model, gemini_timeout_seconds
else:  # `uvicorn main:app` from backend/.
    from config import gemini_api_key, gemini_attempt_timeout_seconds, gemini_fallback_model, gemini_model, gemini_timeout_seconds

app = FastAPI(title="Multimodal Construction Intelligence Platform", version="1.0.0")
logger = logging.getLogger("uvicorn.error")
origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",") if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Finding(StrictModel):
    title: str
    detail: str
    severity: Literal["low", "medium", "high"]
    confidence: float = Field(ge=0, le=1)
    action: str
class ImageAnalysis(StrictModel):
    mode: Literal["safety", "crack"]
    provider: str
    summary: str
    findings: list[Finding]
    limitations: list[str]
class Material(StrictModel):
    name: str
    quantity: float = Field(ge=0)
    unit: str
    rate: float = Field(ge=0)
class PlanRequest(StrictModel):
    area_sqft: float = Field(gt=0, le=10000000)
    location: str = "Tier 1"
    building_type: str = "Residential"
    completion: float = Field(ge=0, le=100)
    budget: float = Field(ge=0)
    materials: list[Material] = Field(default_factory=list)
class PlanResponse(StrictModel):
    provider: str
    completion: float
    estimated_total_inr: float
    budget_inr: float
    budget_variance_inr: float
    cost_breakdown: dict[str, float]
    materials: list[Material]
    assumptions: list[str]
    tasks: list[dict[str, str]]
    summary: str
class AssistantRequest(StrictModel):
    message: str = Field(min_length=1, max_length=4000)
    kind: Literal["chat", "daily_report"] = "chat"
class AssistantResponse(StrictModel):
    provider: str
    answer: str
    guidance: list[str]
class WeatherResponse(StrictModel):
    available: bool
    location: str
    summary: str
    guidance: list[str]

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

def _log_provider_exception(exc: Exception, api_key: str, image_bytes: bytes = b"") -> None:
    """Log a useful exception category and a conservative safe message."""
    if isinstance(exc, json.JSONDecodeError):
        message = f"invalid JSON at line {exc.lineno}, column {exc.colno}"
    elif isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, (TimeoutError, socket.timeout)):
        message = f"Gemini request timed out after {gemini_timeout_seconds():g} seconds"
    elif isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        message = _sanitize_log_text(str(reason), api_key, image_bytes) if isinstance(reason, (str, OSError)) else "network request failed"
    elif isinstance(exc, (TimeoutError, socket.timeout)):
        message = f"Gemini request timed out after {gemini_timeout_seconds():g} seconds"
    elif isinstance(exc, KeyError):
        field = exc.args[0] if exc.args else "unknown"
        message = f"missing provider response field: {field}" if field in {"candidates", "content", "parts", "text"} else "missing provider response field"
    elif isinstance(exc, (ValueError, IndexError, TypeError)):
        message = "invalid provider response structure"
    else:
        message = "details omitted to avoid logging request data"
    logger.warning("Gemini analysis failed: exception_type=%s, safe_message=%s", type(exc).__name__, message)

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
                _log_provider_exception(exc, key, image)
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
    payload = {"contents": [{"parts": parts}], "generationConfig": {"responseMimeType": "application/json"}}
    timeout_budget = gemini_timeout_seconds()
    started_at = time.monotonic()
    deadline = started_at + timeout_budget
    primary_deadline = started_at + timeout_budget / 2
    primary = gemini_model()
    fallback = gemini_fallback_model()
    selected_model = primary
    try:
        response = _gemini_model_request(primary, payload, key, image or b"", primary_deadline)
    except HTTPError as exc:
        if exc.code != 503 or primary == fallback:
            raise
        if deadline - time.monotonic() < 5.0:
            logger.warning("Gemini fallback skipped: model=%s reason=insufficient overall budget", fallback)
            raise
        logger.warning("Gemini fallback starting: model=%s reason=primary HTTP 503 after retries", fallback)
        selected_model = fallback
        response = _gemini_model_request(fallback, payload, key, image or b"", deadline)
    except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
        if not _is_timeout_error(exc) or primary == fallback:
            raise
        if deadline - time.monotonic() < 5.0:
            logger.warning("Gemini fallback skipped: model=%s reason=insufficient overall budget after primary timeout", fallback)
            raise
        logger.warning("Gemini fallback starting: model=%s reason=primary timeout", fallback)
        selected_model = fallback
        response = _gemini_model_request(fallback, payload, key, image or b"", deadline)
    try:
        return json.loads(response)["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as exc:
        _log_provider_exception(exc, key, image or b"")
        logger.warning("Gemini response parsing failed: model=%s", selected_model)
        raise

@app.post("/api/analyze", response_model=ImageAnalysis)
async def analyze(mode: Literal["safety", "crack"], image: UploadFile = File(...)):
    data = await image.read()
    if not image.content_type or not image.content_type.startswith("image/"): raise HTTPException(415, "Upload an image file")
    if len(data) > 12 * 1024 * 1024: raise HTTPException(413, "Image must be 12 MB or smaller")
    instruction = ("Review this construction site photo for visibly observable PPE and hazards. Do not infer hidden facts. Return JSON with summary, findings array of title,detail,severity(low|medium|high),confidence(0..1),action, limitations array. State observations are not verified compliance." if mode == "safety" else "Describe the visible crack pattern, cautious severity and possible causes, and inspection/remediation steps. Never infer precise dimensions without scale. Return JSON summary, findings array(title,detail,severity low|medium|high,confidence 0..1,action),limitations array.")
    try:
        parsed = json.loads(gemini(instruction, data, image.content_type))
        limitations = parsed.get("limitations", [])
        if mode == "safety": limitations.append("Visual observations only; PPE compliance is not verified.")
        if mode == "crack": limitations.append("No precise crack dimensions can be inferred without a scale reference.")
        return ImageAnalysis(mode=mode, provider="Gemini", summary=parsed["summary"], findings=parsed["findings"], limitations=limitations)
    except RuntimeError:
        raise HTTPException(503, "AI analysis unavailable: configure GEMINI_API_KEY. No demo result was generated.")
    except Exception as e:
        # Provider exceptions can include the request URL, which contains the
        # API key as a query parameter. Never return raw provider error text.
        if not isinstance(e, HTTPError) and not _is_timeout_error(e):
            _log_provider_exception(e, gemini_api_key() or "", data)
        raise HTTPException(502, "AI provider error or invalid response")

@app.post("/api/plan", response_model=PlanResponse)
def plan(req: PlanRequest):
    tier = req.location.lower()
    mult = 1.18 if "1" in tier or "metro" in tier else (1.08 if "2" in tier else 1.0)
    base = {"Residential": 1900, "Commercial": 2500, "Industrial": 1700}.get(req.building_type, 1900) * mult
    materials = req.materials or [Material(name="Cement", quantity=round(req.area_sqft * .42, 1), unit="bags", rate=390), Material(name="Steel", quantity=round(req.area_sqft * 3.8, 1), unit="kg", rate=68), Material(name="Sand", quantity=round(req.area_sqft * 1.2, 1), unit="cu ft", rate=55)]
    material_cost = sum(m.quantity * m.rate for m in materials)
    total = max(base * req.area_sqft, material_cost)
    breakdown = {"Materials": round(total * .52), "Labour": round(total * .24), "Services": round(total * .12), "Finishes": round(total * .08), "Contingency": round(total * .04)}
    return PlanResponse(provider="Deterministic estimate", completion=req.completion, estimated_total_inr=round(total), budget_inr=req.budget, budget_variance_inr=round(req.budget-total), cost_breakdown=breakdown, materials=materials, assumptions=["Early-stage benchmark allowance; validate rates, quantities, taxes and specifications locally.", "Location tier adjusts the baseline allowance; site access, design and market movement are not modeled.", "Figures are estimates only, not a professional valuation, tender or quotation."], tasks=[{"task":"Site setup & foundations","duration":"Weeks 1–4","resource":"Civil crew, excavator"},{"task":"Structure & slab","duration":"Weeks 5–12","resource":"Formwork, steel, concrete crew"},{"task":"MEP rough-in & masonry","duration":"Weeks 13–18","resource":"Electrical, plumbing, masonry teams"},{"task":"Finishes & handover","duration":"Weeks 19–24","resource":"Finishing crew, QA"}], summary=f"At {req.completion:.0f}% completion, estimated remaining allowance is ₹{total*(100-req.completion)/100:,.0f}. Confirm quantities and local rates before commitment.")

@app.post("/api/assistant", response_model=AssistantResponse)
def assistant(req: AssistantRequest):
    if gemini_api_key():
        try:
            prompt = ("Prepare a concise daily construction site report using only supplied notes; flag missing details: " if req.kind == "daily_report" else "Answer cautiously; do not invent local regulations: ") + req.message
            answer = gemini(prompt)
            try: answer = json.loads(answer).get("answer", answer)
            except Exception: pass
            return AssistantResponse(provider="Gemini", answer=str(answer), guidance=[])
        except Exception: pass
    if req.kind == "daily_report": return AssistantResponse(provider="Guided template", answer="Draft structure (fill only from site records):", guidance=["Date / site / verified weather", "Work completed and measured quantities", "Crew and equipment", "Deliveries and inspections", "Safety observations and corrective actions", "Delays and next shift plan"])
    return AssistantResponse(provider="Offline guidance", answer="I can provide general construction guidance. Include project type, location and relevant dimensions for a more useful answer. This is not an engineering determination.", guidance=["For structural movement, isolate immediate hazards and consult a qualified local structural engineer.", "Confirm applicable local requirements with the responsible professional."])

@app.get("/api/weather", response_model=WeatherResponse)
def weather(latitude: float = -1, longitude: float = -1, location: str = "site"):
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180): return WeatherResponse(available=False, location=location, summary="Live weather unavailable: provide valid coordinates.", guidance=["Check a trusted local forecast before outdoor work."])
    try:
        url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({"latitude": latitude, "longitude": longitude, "current": "temperature_2m,precipitation,wind_speed_10m", "timezone": "auto"})
        with urllib.request.urlopen(url, timeout=8) as r: current = json.loads(r.read())["current"]
        return WeatherResponse(available=True, location=location, summary=f"Current: {current['temperature_2m']}°C, precipitation {current['precipitation']} mm, wind {current['wind_speed_10m']} km/h.", guidance=["Use site-specific thresholds for lifting and work at height; weather alone does not establish safe operation."])
    except Exception: return WeatherResponse(available=False, location=location, summary="Live weather data is temporarily unavailable.", guidance=["Check a trusted local forecast before outdoor work."])
