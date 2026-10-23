# Multimodal Construction Intelligence Platform

A fresh React + Vite and FastAPI MVP for field observations, preliminary construction estimates, and site guidance.

## Features

- Gemini multimodal image analysis for visible site safety observations and crack patterns. Provider results are schema validated. With no key configured, image analysis clearly returns unavailable instead of generating a fake result.
- Deterministic early-stage INR estimate, editable project inputs and material quantities, cost breakdown, schedule and resource plan.
- Construction assistant and daily report template. Offline guidance is labeled; Gemini answers require a configured key.
- Open-Meteo current conditions endpoint for valid coordinates. Weather is reported unavailable when coordinates or service are unavailable.
- CORS configured through environment variables and strict Pydantic response models.

AI visual observations do not verify compliance or replace site inspection. Crack dimensions are not inferred from ordinary photos without a scale. All costs are estimates, not professional valuations or quotations.

## Local setup (PowerShell)

Terminal 1, backend:

```powershell
cd backend
Copy-Item .env.example .env
# Optional: edit .env and set GEMINI_API_KEY (key remains backend-only)
.\.venv-new\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Terminal 2, frontend:

```powershell
cd frontend
npm run dev
```

Open http://localhost:5173. API docs: http://localhost:8000/docs. To point the UI at a hosted API, set `VITE_API_URL` in `frontend/.env` before building.

## Tests and build

```powershell
cd backend
python -m pytest
cd ..\frontend
npm run build
```

## GitHub and deployment

Keep `.env`, `.venv`, `node_modules`, and build output out of Git (covered by `.gitignore`). Create an empty GitHub repository, then from this workspace run `git init`, `git add README.md .gitignore backend frontend`, `git commit -m "Build construction intelligence MVP"`, `git branch -M main`, `git remote add origin <YOUR_REPOSITORY_URL>`, and `git push -u origin main`.

Deploy the backend as a Python web service with root `backend`, install `requirements.txt`, and start `uvicorn main:app --host 0.0.0.0 --port $PORT`. Set `GEMINI_API_KEY`, optional `GEMINI_MODEL`, and `CORS_ORIGINS` to the deployed frontend origin. Deploy `frontend` as a static site using `npm ci` and `npm run build`, publish `dist`, and set `VITE_API_URL` to the backend URL at build time. Never put the Gemini key in frontend variables.
