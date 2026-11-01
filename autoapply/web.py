"""Local FastAPI interface for the Autoply workflow.

The server intentionally binds to localhost only. Cover-letter generation
sends extracted résumé text, résumé links, and the job description to the
Google Gemini API. Review Google's current data-handling terms before use.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv, set_key
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from autoapply.autoapply import JobScraper
from autoapply.drafts import create_drafts
from autoapply.helper.excel_handler import ExcelHandler
from autoapply.helper.genai import CoverLetterGenerator


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
TEMPLATES = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))
TRUSTED_ORIGIN = "http://127.0.0.1:8000"
CSRF_TOKEN = secrets.token_urlsafe(32)


class WorkflowState:
    """Small in-memory state store for one local Autoply session."""

    def __init__(self) -> None:
        self.jobs: list[dict[str, str]] = []
        self.drafts: list[dict[str, str]] = []
        self.is_running = False
        self.is_drafting = False
        self.message = "Configure Autoply, then import jobs from Wellfound."
        self.error = False
        self.filters = {"country": "India", "keyword": "", "remote_only": "false", "company_size": "", "employment_type": "", "limit": "30"}
        self.lock = threading.Lock()

    def set_message(self, message: str, *, error: bool = False) -> None:
        with self.lock:
            self.message = message
            self.error = error


state = WorkflowState()


def is_trusted_loopback_origin(origin: str | None) -> bool:
    """Allow only loopback HTTP origins so the dashboard stays local-only."""
    if not origin:
        return False

    try:
        hostname = urlsplit(origin).hostname
    except ValueError:
        return False

    if not hostname:
        return False

    hostname = hostname.lower().rstrip(".")
    if hostname == "localhost":
        return True

    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def verify_csrf(request: Request, csrf_token: str) -> None:
    """Reject dashboard POSTs that do not originate from this local server."""
    origin = request.headers.get("origin") or request.headers.get("referer")
    if (
        not is_trusted_loopback_origin(origin)
        or not hmac.compare_digest(csrf_token, CSRF_TOKEN)
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token or origin.")


def load_configuration() -> dict[str, str]:
    load_dotenv(ENV_FILE, override=True)
    return {
        "api_key": os.getenv("API_KEY", ""),
        "profile_path": os.getenv("PROFILE_PATH", ""),
        "resume_path": os.getenv("RESUME_PATH", ""),
        "auto_submit": os.getenv("AUTO_SUBMIT", "false").lower() == "true",
    }


def save_configuration(api_key: str, profile_path: str, resume_path: str) -> None:
    """Persist only local settings; a blank API-key field preserves the old key."""
    ENV_FILE.touch(exist_ok=True)
    if api_key.strip():
        set_key(str(ENV_FILE), "API_KEY", api_key.strip())
        os.environ["API_KEY"] = api_key.strip()
    if profile_path.strip():
        set_key(str(ENV_FILE), "PROFILE_PATH", profile_path.strip())
        os.environ["PROFILE_PATH"] = profile_path.strip()
    if resume_path.strip():
        set_key(str(ENV_FILE), "RESUME_PATH", resume_path.strip())
        os.environ["RESUME_PATH"] = resume_path.strip()
    if not os.getenv("AUTO_SUBMIT"):
        set_key(str(ENV_FILE), "AUTO_SUBMIT", "false")
        os.environ["AUTO_SUBMIT"] = "false"


def profile_configuration_errors(config: dict[str, str]) -> list[str]:
    errors = []
    profile_path = config["profile_path"]
    if not profile_path:
        errors.append("Add a Firefox profile path.")
    elif not Path(profile_path).exists():
        errors.append(f"Firefox profile path was not found: {profile_path}")
    return errors


def configuration_errors(config: dict[str, str]) -> list[str]:
    errors = []
    if not config["api_key"]:
        errors.append("Add a Gemini API key.")
    errors.extend(profile_configuration_errors(config))
    resume_path = config["resume_path"]
    if not resume_path:
        errors.append("Add a résumé path.")
    elif not Path(resume_path).exists():
        errors.append(f"Résumé path was not found: {resume_path}")
    return errors


def scrape_jobs_in_background(config: dict[str, str], filters: dict[str, str]) -> None:
    try:
        scraper = JobScraper(profile_path=config["profile_path"], progress_callback=state.set_message)
        try:
            scraper.scrape_jobs(maximum_jobs=int(filters["limit"]), filters=filters)
            jobs = [{**job, "id": str(index)} for index, job in enumerate(scraper.data)]
            with state.lock:
                state.jobs = jobs
                state.drafts = []
                state.message = (
                    f"Imported {len(jobs)} job listings with descriptions."
                    f"{scraper.scrape_warning} Select roles to create drafts."
                )
                state.error = False
        finally:
            scraper.close()
    except Exception as exc:
        state.set_message(f"Could not import jobs: {exc}", error=True)
    finally:
        with state.lock:
            state.is_running = False


def create_drafts_in_background(jobs: list[dict[str, str]], config: dict[str, str]) -> None:
    """Generate local drafts and update the dashboard when the work completes."""
    try:
        generator = CoverLetterGenerator(
            api_key=config["api_key"],
            resume_path=config["resume_path"],
        )
        workbook = ExcelHandler(str(PROJECT_ROOT / "job_applications.xlsx"))
        drafts, failures = create_drafts(jobs, generator, workbook)
        with state.lock:
            state.drafts = drafts
            if drafts:
                state.message = (
                    f"Created {len(drafts)} cover-letter draft(s) and saved them to "
                    "job_applications.xlsx. No applications were submitted."
                )
                state.error = False
            else:
                state.message = "Could not create any cover-letter drafts. " + " ".join(failures[:3])
                state.error = True
            if failures and drafts:
                state.message += f" Skipped {len(failures)} role(s) that could not be drafted."
    except Exception as exc:
        state.set_message(f"Could not create cover-letter drafts: {exc}", error=True)
    finally:
        with state.lock:
            state.is_drafting = False


def create_app() -> FastAPI:
    app = FastAPI(title="Autoply", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")

    @app.get("/")
    def dashboard(request: Request):
        config = load_configuration()
        with state.lock:
            jobs, drafts = list(state.jobs), list(state.drafts)
            message, error = state.message, state.error
            is_running, is_drafting = state.is_running, state.is_drafting
        return TEMPLATES.TemplateResponse(request, "index.html", {
            "config": config, "jobs": jobs, "drafts": drafts, "message": message, "error": error,
            "is_running": is_running, "config_errors": configuration_errors(config), "filters": state.filters,
            "is_drafting": is_drafting,
            "csrf_token": CSRF_TOKEN,
        })

    @app.post("/settings")
    def update_settings(
        request: Request,
        csrf_token: str = Form(""),
        api_key: str = Form(""),
        profile_path: str = Form(""),
        resume_path: str = Form(""),
    ):
        verify_csrf(request, csrf_token)
        save_configuration(api_key, profile_path, resume_path)
        state.set_message("Local settings saved. Your API key is never shown in the dashboard.")
        return RedirectResponse(url="/", status_code=303)

    @app.post("/jobs/import")
    def import_jobs(
        request: Request, csrf_token: str = Form(""),
        country: str = Form("India"), keyword: str = Form(""), remote_only: str = Form("false"),
        company_size: str = Form(""), employment_type: str = Form(""), limit: str = Form("30"),
    ):
        verify_csrf(request, csrf_token)
        config = load_configuration()
        errors = profile_configuration_errors(config)
        if errors:
            state.set_message(" ".join(errors), error=True)
            return RedirectResponse(url="/", status_code=303)
        filters = {"country": country.strip(), "keyword": keyword.strip(), "remote_only": remote_only,
                   "company_size": company_size, "employment_type": employment_type, "limit": limit}
        with state.lock:
            if state.is_running:
                state.message = "An import is already in progress."
                return RedirectResponse(url="/", status_code=303)
            state.is_running, state.error = True, False
            state.jobs = []
            state.filters = filters
            state.message = "Opening Firefox and importing jobs. Keep this page open, then refresh shortly."
        threading.Thread(target=scrape_jobs_in_background, args=(config, filters), daemon=True).start()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/applications/draft")
    def draft_applications(
        request: Request,
        csrf_token: str = Form(""),
        job_ids: list[str] = Form(default=[]),
    ):
        verify_csrf(request, csrf_token)
        selected = set(job_ids)
        with state.lock:
            selected_jobs = [job for job in state.jobs if job["id"] in selected]
        if not selected_jobs:
            state.set_message("Select at least one job before drafting applications.", error=True)
        else:
            config = load_configuration()
            errors = configuration_errors(config)
            if errors:
                state.set_message(" ".join(errors), error=True)
                return RedirectResponse(url="/", status_code=303)
            with state.lock:
                if state.is_drafting:
                    state.message = "Cover-letter drafting is already in progress."
                    return RedirectResponse(url="/", status_code=303)
                state.is_drafting = True
                state.error = False
                state.message = f"Creating cover-letter drafts for {len(selected_jobs)} role(s)…"
            threading.Thread(
                target=create_drafts_in_background,
                args=(selected_jobs, config),
                daemon=True,
            ).start()
        return RedirectResponse(url="/", status_code=303)

    return app


def run() -> None:
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=8000)
