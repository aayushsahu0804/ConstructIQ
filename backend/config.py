"""Backend environment loading and shared configuration accessors."""
import math
import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent
ENV_FILE = BACKEND_DIR / ".env"


def load_environment(dotenv_path: Path | None = None) -> None:
    """Load a selected dotenv file without overriding explicitly exported variables."""
    load_dotenv(dotenv_path=dotenv_path or ENV_FILE, override=False)


# Resolve from this file, not the process working directory. This works when
# Uvicorn is launched from either the repository root or backend/.
load_environment()


def gemini_api_key() -> str | None:
    """Return the configured key to server-side callers only."""
    return os.getenv("GEMINI_API_KEY")


def gemini_model() -> str:
    model = (os.getenv("GEMINI_MODEL") or "").strip()
    if not model or model == "gemini-3.8-flash":
        return "gemini-3.5-flash-lite"
    return model


def gemini_timeout_seconds() -> float:
    """Return the bounded overall Gemini request budget (default 90 seconds)."""
    try:
        timeout = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "90"))
    except (TypeError, ValueError):
        timeout = 90.0
    if not math.isfinite(timeout):
        timeout = 90.0
    return min(max(timeout, 15.0), 300.0)


def gemini_attempt_timeout_seconds() -> float:
    """Return the bounded maximum socket timeout for one provider attempt."""
    try:
        timeout = float(os.getenv("GEMINI_ATTEMPT_TIMEOUT_SECONDS", "30"))
    except (TypeError, ValueError):
        timeout = 30.0
    if not math.isfinite(timeout):
        timeout = 30.0
    return min(max(timeout, 5.0), 60.0)
