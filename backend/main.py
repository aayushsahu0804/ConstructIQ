from __future__ import annotations
import base64, json, logging, os, random, re, socket, time, urllib.parse, urllib.request
from typing import Literal
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from urllib.error import HTTPError, URLError
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
class AssistantRequest(StrictModel):
    message: str = Field(min_length=1, max_length=4000)
    kind: Literal["chat", "daily_report"] = "chat"
class AssistantResponse(StrictModel):
    provider: str
    answer: str
    guidance: list[str]
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
        if exc.code not in {429, 503} or primary == fallback:
            raise
        if deadline - time.monotonic() < 5.0:
            logger.warning("Gemini fallback skipped: model=%s reason=insufficient overall budget", fallback)
            raise
        reason = "primary quota exhausted (HTTP 429)" if exc.code == 429 else "primary HTTP 503 after retries"
        logger.warning("Gemini fallback starting: model=%s reason=%s", fallback, reason)
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

def _weather_json(url: str, stage: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            payload = json.loads(response.read())
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
