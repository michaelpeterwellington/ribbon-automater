import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api import audit, cert_jobs, certificates, customers, devices, firmware, settings_api, upgrades
from app.database import init_db
from app.services.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Ribbon SBC Upgrade Automation", lifespan=lifespan)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── API routers ────────────────────────────────────────────────────────────
app.include_router(customers.router)
app.include_router(devices.router)
app.include_router(firmware.router)
app.include_router(certificates.router)
app.include_router(upgrades.router)
app.include_router(cert_jobs.router)
app.include_router(settings_api.router)
app.include_router(audit.router)


# ── Health check ───────────────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    return {"status": "ok"}


# ── Web UI routes ──────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/customers", response_class=HTMLResponse)
async def customers_page(request: Request):
    return templates.TemplateResponse("customers.html", {"request": request})


@app.get("/customers/{customer_id}", response_class=HTMLResponse)
async def customer_detail_page(request: Request, customer_id: int):
    return templates.TemplateResponse(
        "customer_detail.html", {"request": request, "customer_id": customer_id}
    )


@app.get("/devices", response_class=HTMLResponse)
async def devices_page(request: Request):
    return templates.TemplateResponse("devices.html", {"request": request})


@app.get("/firmware", response_class=HTMLResponse)
async def firmware_page(request: Request):
    return templates.TemplateResponse("firmware.html", {"request": request})


@app.get("/upgrades", response_class=HTMLResponse)
async def upgrades_page(request: Request):
    return templates.TemplateResponse("upgrades.html", {"request": request})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request})


@app.get("/certificates", response_class=HTMLResponse)
async def certificates_page(request: Request):
    return templates.TemplateResponse("certificates.html", {"request": request})


@app.get("/cert-jobs", response_class=HTMLResponse)
async def cert_jobs_page(request: Request):
    return templates.TemplateResponse("cert_jobs.html", {"request": request})


@app.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request):
    return templates.TemplateResponse("audit.html", {"request": request})
