# Ribbon SBC Upgrade Automation

A web-based platform for automating firmware upgrades across managed Ribbon SBC devices. Supports SWe Edge, SBC 1000, and SBC 2000 device types.

## Features

- **Firmware library** — Upload firmware files once, deploy to any device
- **Customer management** — Organise devices per customer
- **Scheduled upgrades** — Queue jobs for immediate execution or a future time
- **Live upgrade log** — Real-time streaming log view per job
- **Version checking** — Live probe any device to fetch its current firmware version
- **Email notifications** — SMTP alerts on job completion or failure
- **Encrypted credentials** — Device passwords stored at rest using Fernet symmetric encryption

## How It Works

Ribbon SBCs expose no REST API for firmware operations. This platform automates the device's own WebUI by replaying the same CGI/PHP HTTP requests a browser makes:

1. Three-step login (`/cgi/index.php` → splash acknowledgement → `/cgi/login/login_do.php`)
2. Scrape the upgrade page for hidden `__m_*` form fields
3. Pre-upgrade validation (`validateSwUpgrade.php`)
4. Configuration backup (`configBackup.php`)
5. Firmware upload (`swDownload_do.phpx`) — device installs and reboots automatically
6. Poll until device comes back online (~5–10 min)
7. Re-login and verify new firmware version

## Tech Stack

| Component | Technology |
|---|---|
| Backend | Python 3.11, FastAPI |
| Database | SQLite + SQLAlchemy 2 async (aiosqlite) |
| Scheduler | APScheduler 3.x (SQLite-backed, survives restarts) |
| SBC client | httpx async + BeautifulSoup4 |
| Email | aiosmtplib |
| Frontend | Jinja2 + Bootstrap 5 + vanilla JS |
| Deployment | Docker Compose |

## Quick Start

### Docker (recommended)

```bash
git clone https://github.com/michaelpeterwellington/ribbon-automater.git ribbon-automation
cd ribbon-automation

# Generate a Fernet encryption key and configure the environment
cp .env.example .env
SECRET=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
sed -i "s|REPLACE_ME_WITH_FERNET_KEY|${SECRET}|" .env

# Build and start
docker compose up -d --build

# View logs
docker compose logs -f
```

Open **http://localhost:8000** in your browser.

### Local / Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure environment (edit paths for local use)
cp .env.example .env
# Edit .env: set DB_PATH and UPLOAD_DIR to local paths, add SECRET_KEY

uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Configuration (`.env`)

| Variable | Description |
|---|---|
| `SECRET_KEY` | Fernet key for encrypting stored passwords. Generate with: `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `DB_PATH` | Path to the SQLite database file (e.g. `/data/ribbon.db`) |
| `UPLOAD_DIR` | Directory for uploaded firmware files (e.g. `/uploads`) |
| `APP_HOST` | Bind address (default `0.0.0.0`) |
| `APP_PORT` | Port (default `8000`) |
| `DEBUG` | Set `true` to enable FastAPI debug mode |

## Usage

1. **Add a customer** — Customers page → Add Customer
2. **Add devices** — Customer detail page → Add Device (IP, credentials, device type)
3. **Upload firmware** — Firmware page → drag and drop or browse for `.img` file
4. **Check device version** — Devices page → click the refresh icon on any row
5. **Queue an upgrade** — Devices page (or Customer detail) → click the upgrade icon → select firmware and optional schedule time
6. **Monitor progress** — Upgrades page → click any job to view the live log

## Project Structure

```
app/
├── main.py                  # FastAPI app + lifespan (DB init, scheduler)
├── config.py                # Settings from .env
├── database.py              # Async SQLAlchemy engine
├── models.py                # ORM models
├── schemas.py               # Pydantic request/response schemas
├── api/
│   ├── customers.py
│   ├── devices.py
│   ├── firmware.py
│   ├── upgrades.py
│   └── settings_api.py
└── services/
    ├── ribbon_client.py     # Ribbon SBC web-session automation
    ├── upgrade_service.py   # Upgrade orchestration (8-step workflow)
    ├── scheduler.py         # APScheduler singleton
    ├── crypto.py            # Fernet encrypt/decrypt
    └── notifications.py     # Email alerts
```

## Notes

- TLS certificate verification is disabled for SBC connections (`verify=False`) — Ribbon devices use self-signed certificates
- The platform has no built-in authentication and is intended for use on an internal/private network. Add a reverse proxy (nginx, Caddy, Traefik) with basic auth or mTLS if external access is needed
- Scheduled jobs persist across restarts via APScheduler's SQLite job store
- Upgrade logs are stored in the database and viewable at any time after the job completes
