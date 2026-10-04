"""Local Admin Web Dashboard for RAVEN.

Run with:
    python dashboard.py

Then visit http://localhost:8000 in your browser.

Displays:
- Total registered users and breakdown by tier (FREE/PRO/ENTERPRISE).
- Mock revenue metrics (based on upgraded PRO users).
- System health: Ollama status, active Telegram bot status, recent error logs from bot.log.
"""

import asyncio
import json
import os
import secrets
import subprocess
import sys
from datetime import datetime
from typing import Any, Dict, List

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Import the database module to get user stats
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import database

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Initialize FastAPI app
app = FastAPI(title="RAVEN Local Admin Dashboard")

# Set up templates and static files. These are resolved from BASE_DIR, not the
# process working directory, so `uvicorn dashboard:app` behaves the same as
# `python dashboard.py` no matter where it is launched from. The directories are
# created first because StaticFiles() validates its target at construction time,
# which happens during import - a later makedirs() would be too late.
_STATIC_DIR = os.path.join(BASE_DIR, "dashboard", "static")
_TEMPLATE_DIR = os.path.join(BASE_DIR, "dashboard", "templates")
os.makedirs(_TEMPLATE_DIR, exist_ok=True)
os.makedirs(_STATIC_DIR, exist_ok=True)

templates = Jinja2Templates(directory=_TEMPLATE_DIR)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

# This dashboard renders customer tier counts, revenue projections and raw
# bot.log lines. Binding to localhost is not an authorization control on its
# own: any local process can reach it, and a browser can be pointed at it via
# DNS rebinding. Require a shared token when one is configured.
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()
DASHBOARD_ALLOWED_HOSTS = os.getenv(
    "DASHBOARD_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver"
).split(",")


def _require_auth(request: Request) -> None:
    """Reject the request unless it presents DASHBOARD_TOKEN."""
    if not DASHBOARD_TOKEN:
        return
    supplied = (request.headers.get("x-dashboard-token")
                or request.query_params.get("token", ""))
    # compare_digest avoids leaking the token through response timing.
    if not secrets.compare_digest(supplied, DASHBOARD_TOKEN):
        raise HTTPException(status_code=401, detail="dashboard token required")


def get_ollama_status() -> Dict[str, Any]:
    """Check Ollama service status and list running models.
    Returns a dict with keys: 'running' (bool), 'models' (list of str), 'error' (str or None).
    """
    try:
        # Run `ollama ps` to see running models
        result = subprocess.run(
            ["ollama", "ps"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            # Parse output: first line is header, subsequent lines are models
            lines = result.stdout.strip().split("\n")
            if len(lines) <= 1:
                return {"running": True, "models": [], "error": None}
            # Skip header
            models = []
            for line in lines[1:]:
                # Format: NAME      ID       SIZE   PROCESSOR   UNTIL
                parts = line.split()
                if parts:
                    models.append(parts[0])
            return {"running": True, "models": models, "error": None}
        else:
            # Ollama not running or command failed
            return {
                "running": False,
                "models": [],
                "error": result.stderr.strip() or "Unknown error",
            }
    except FileNotFoundError:
        return {"running": False, "models": [], "error": "Ollama not installed"}
    except subprocess.TimeoutExpired:
        return {"running": False, "models": [], "error": "Timeout checking Ollama"}
    except Exception as e:
        return {"running": False, "models": [], "error": str(e)}


def get_telegram_bot_status() -> Dict[str, Any]:
    """Check if the Telegram bot (raven.py) is running.
    Returns a dict with keys: 'running' (bool), 'pid' (int or None), 'error' (str or None).
    """
    try:
        # Use tasklist on Windows or ps on Linux/macOS
        if sys.platform.startswith("win"):
            cmd = ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"]
        else:
            cmd = ["ps", "aux"]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return {
                "running": False,
                "pid": None,
                "error": f"Failed to check processes: {result.stderr.strip()}",
            }

        output = result.stdout
        # Look for raven.py in the command line
        found = False
        pid = None
        for line in output.split("\n"):
            if "raven.py" in line:
                found = True
                # Extract PID: on Windows, the second column in CSV is PID
                if sys.platform.startswith("win"):
                    # CSV format: "Image Name","PID","Session Name","Session#","Mem Usage"
                    parts = line.split(",")
                    if len(parts) >= 2:
                        try:
                            pid = int(parts[1].strip('"'))
                        except ValueError:
                            pass
                else:
                    # Linux/macOS: PID is second column in ps aux
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            pid = int(parts[1])
                        except ValueError:
                            pass
                break

        if found:
            return {"running": True, "pid": pid, "error": None}
        else:
            return {"running": False, "pid": None, "error": "Telegram bot process not found"}
    except Exception as e:
        return {"running": False, "pid": None, "error": str(e)}


def get_recent_errors(lines: int = 10) -> List[str]:
    """Read recent lines from bot.log and return those containing error indicators.
    Returns a list of strings (the matching lines).
    """
    log_file = os.path.join(BASE_DIR, "bot.log")
    if not os.path.exists(log_file):
        return [f"Log file {log_file} not found"]
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            # Read last N lines
            lines_list = f.readlines()
            last_lines = lines_list[-lines:] if len(lines_list) >= lines else lines_list
            # Filter for lines that contain common error indicators
            error_lines = [
                line.strip()
                for line in last_lines
                if any(
                    marker in line.lower()
                    for marker in ("error", "exception", "traceback", "failed", "critical")
                )
            ]
            return error_lines if error_lines else ["No recent errors found in log"]
    except Exception as e:
        return [f"Error reading log file: {str(e)}"]


async def get_user_stats() -> Dict[str, Any]:
    """Get user statistics from the database."""
    try:
        stats = await database.astats()
        # stats already contains total_users and by_tier
        return stats
    except Exception as e:
        return {"total_users": 0, "by_tier": {}, "error": str(e)}


def calculate_mock_revenue(by_tier: Dict[str, int]) -> Dict[str, Any]:
    """Calculate mock revenue based on PRO users.
    Assume:
        FREE: $0
        PRO: $10 per user per month
        ENTERPRISE: $0 (we don't have pricing for enterprise, or could be custom)
    Returns dict with monthly revenue and yearly projection.
    """
    pro_count = by_tier.get(database.TIER_PRO, 0)
    monthly = pro_count * 10  # $10 per PRO user
    yearly = monthly * 12
    return {
        "pro_users": pro_count,
        "monthly_revenue_usd": monthly,
        "yearly_revenue_usd": yearly,
        "currency": "USD",
        "note": "Mock revenue based on $10/month per PRO user",
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Render the dashboard page."""
    _require_auth(request)

    # Gather data concurrently
    user_stats_task = asyncio.create_task(get_user_stats())
    ollama_task = asyncio.create_task(asyncio.to_thread(get_ollama_status))
    telegram_task = asyncio.create_task(asyncio.to_thread(get_telegram_bot_status))
    errors_task = asyncio.create_task(asyncio.to_thread(get_recent_errors))

    # Wait for all
    user_stats = await user_stats_task
    ollama_status = await ollama_task
    telegram_status = await telegram_task
    recent_errors = await errors_task

    # Calculate mock revenue
    by_tier = user_stats.get("by_tier", {})
    mock_revenue = calculate_mock_revenue(by_tier)

    # Prepare context for template
    context = {
        "request": request,
        "title": "RAVEN Local Admin Dashboard",
        "user_stats": user_stats,
        "ollama_status": ollama_status,
        "telegram_status": telegram_status,
        "recent_errors": recent_errors,
        "mock_revenue": mock_revenue,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return templates.TemplateResponse(request, "dashboard.html", context)


@app.get("/api/stats")
async def api_stats(request: Request):
    """JSON endpoint for stats (useful for AJAX updates)."""
    _require_auth(request)

    user_stats = await get_user_stats()
    ollama_status = await asyncio.to_thread(get_ollama_status)
    telegram_status = await asyncio.to_thread(get_telegram_bot_status)
    recent_errors = await asyncio.to_thread(get_recent_errors)
    by_tier = user_stats.get("by_tier", {})
    mock_revenue = calculate_mock_revenue(by_tier)

    return JSONResponse(
        {
            "user_stats": user_stats,
            "ollama_status": ollama_status,
            "telegram_status": telegram_status,
            "recent_errors": recent_errors,
            "mock_revenue": mock_revenue,
            "timestamp": datetime.now().isoformat(),
        }
    )


def main():
    """Run the dashboard server.

    Refuses to start without DASHBOARD_TOKEN rather than serving customer
    counts and log lines to anything that can reach localhost.
    """
    if not DASHBOARD_TOKEN:
        raise SystemExit(
            "DASHBOARD_TOKEN is not set.\n"
            "This dashboard exposes customer tier counts and bot.log contents, "
            "so it refuses to start unauthenticated.\n"
            "Set it in .env, e.g. DASHBOARD_TOKEN=" + secrets.token_urlsafe(24)
        )
    print(f"RAVEN dashboard on http://localhost:8000  (token required: {DASHBOARD_TOKEN})")
    uvicorn.run("dashboard:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()