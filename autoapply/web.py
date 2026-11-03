"""Local FastAPI interface for the Autowell workflow.

The server intentionally binds to localhost only. Cover-letter generation
sends extracted resume text, resume links, and the job description to the
Google Gemini API. Review Google's current data-handling terms before use.
"""

from __future__ import annotations

import hmac
import os
import secrets
import threading
from pathlib import Path

from dotenv import load_dotenv, set_key
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from autoapply.autoapply import JobScraper
from autoapply.helper.excel_handler import ExcelHandler
from autoapply.helper.gotolink import Gotolink


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
TEMPLATES = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))
CSRF_COOKIE_NAME = "autowell_csrf"


class WorkflowState:
    """Small in-memory state store for one local Autowell session."""

    def __init__(self) -> None:
        self.jobs: list[dict[str, str]] = []
        self.submitted_job_links: set[str] = set()
        self.is_running = False
        self.message = "Configure Autowell, then import jobs from Wellfound."
        self.error = False
        self.filters = {"country": "India", "keyword": "", "remote_only": "false", "company_size": "", "employment_type": "", "limit": "30"}
        self.lock = threading.Lock()

    def set_message(self, message: str, *, error: bool = False) -> None:
        with self.lock:
            self.message = message
            self.error = error


state = WorkflowState()


def load_configuration() -> dict[str, str]:
    load_dotenv(ENV_FILE, override=True)
    return {
        "api_key": os.getenv("API_KEY", ""),
        "profile_path": os.getenv("PROFILE_PATH", ""),
        "resume_path": os.getenv("RESUME_PATH", ""),
        "application_phone": os.getenv("APPLICATION_PHONE", ""),
        "application_referred": os.getenv("APPLICATION_REFERRED", ""),
        "application_referral_details": os.getenv("APPLICATION_REFERRAL_DETAILS", ""),
        "application_visa_sponsorship": os.getenv("APPLICATION_VISA_SPONSORSHIP", ""),
        "application_relocation": os.getenv("APPLICATION_RELOCATION", ""),
        "auto_submit": os.getenv("AUTO_SUBMIT", "false").lower() == "true",
    }


def save_configuration(
    api_key: str,
    profile_path: str,
    resume_path: str,
    application_phone: str = "",
    application_referred: str = "",
    application_referral_details: str = "",
    application_visa_sponsorship: str = "",
    application_relocation: str = "",
) -> None:
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
    application_profile = {
        "APPLICATION_PHONE": application_phone.strip(),
        "APPLICATION_REFERRED": application_referred.strip(),
        "APPLICATION_REFERRAL_DETAILS": application_referral_details.strip(),
        "APPLICATION_VISA_SPONSORSHIP": application_visa_sponsorship.strip(),
        "APPLICATION_RELOCATION": application_relocation.strip(),
    }
    for key, value in application_profile.items():
        set_key(str(ENV_FILE), key, value)
        os.environ[key] = value
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
        errors.append("Add a resume path.")
    elif not Path(resume_path).exists():
        errors.append(f"Resume path was not found: {resume_path}")
    return errors


def validate_csrf_token(request: Request, submitted_token: str) -> None:
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")
    if not cookie_token or not submitted_token or not hmac.compare_digest(
        cookie_token, submitted_token
    ):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")


def scrape_jobs_in_background(config: dict[str, str], filters: dict[str, str]) -> None:
    try:
        scraper = JobScraper(profile_path=config["profile_path"], progress_callback=state.set_message)
        try:
            scraper.scrape_jobs(maximum_jobs=int(filters["limit"]), filters=filters)
            jobs = [{**job, "id": str(index)} for index, job in enumerate(scraper.data)]
            with state.lock:
                state.jobs = jobs
                state.submitted_job_links.clear()
                state.message = (
                    f"Imported {len(jobs)} Wellfound job cards. Open listings to review full descriptions."
                    f"{scraper.scrape_warning} Select roles to generate and submit applications."
                )
                state.error = False
        finally:
            scraper.close()
    except Exception as exc:
        state.set_message(f"Could not import jobs: {exc}", error=True)
    finally:
        with state.lock:
            state.is_running = False


def submit_applications_in_background(
    config: dict[str, str],
    selected_jobs: list[dict[str, str]],
) -> None:
    processor = None
    try:
        processor = Gotolink(
            auto_submit=True,
            api_key=config["api_key"],
            resume_path=config["resume_path"],
            excel_path=str(PROJECT_ROOT / "job_applications.xlsx"),
            progress_callback=state.set_message,
            profile_answers={
                "phone": config["application_phone"],
                "referred": config["application_referred"],
                "referral_details": config["application_referral_details"],
                "visa_sponsorship": config["application_visa_sponsorship"],
                "relocation": config["application_relocation"],
            },
        )
        results = processor.process_selected_jobs(selected_jobs)
        submitted = sum(was_submitted for _, was_submitted, _ in results)
        failures = [
            (link, error)
            for link, _, error in results
            if error is not None
        ]
        with state.lock:
            state.submitted_job_links.update(
                link for link, was_submitted, _ in results if was_submitted
            )
        message = f"Confirmed submission of {submitted} of {len(results)} selected application(s)."
        if failures:
            details = "; ".join(f"{link}: {error}" for link, error in failures[:3])
            message += f" Failed: {details}"
        state.set_message(message, error=bool(failures))
    except Exception as exc:
        error = str(exc).strip() or type(exc).__name__
        if processor is None:
            try:
                workbook = ExcelHandler(str(PROJECT_ROOT / "job_applications.xlsx"))
                for job in selected_jobs:
                    workbook.add_application(
                        job["link"],
                        "",
                        "",
                        "Failed",
                        f"Application processing could not start: {error}",
                    )
                workbook.save_excel()
            except Exception as logging_error:
                error += f"; could not save failure records: {logging_error}"
        state.set_message(f"Could not submit applications: {error}", error=True)
    finally:
        with state.lock:
            state.is_running = False


def create_app() -> FastAPI:
    app = FastAPI(title="Autowell", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")

    @app.get("/")
    def dashboard(request: Request):
        config = load_configuration()
        csrf_token = request.cookies.get(CSRF_COOKIE_NAME) or secrets.token_urlsafe(32)
        with state.lock:
            jobs, message, error, is_running = list(state.jobs), state.message, state.error, state.is_running
            submitted_job_links = set(state.submitted_job_links)
        response = TEMPLATES.TemplateResponse(request, "index.html", {
            "config": config, "jobs": jobs, "message": message, "error": error,
            "is_running": is_running, "config_errors": configuration_errors(config), "filters": state.filters,
            "submitted_job_links": submitted_job_links, "csrf_token": csrf_token,
        })
        response.set_cookie(
            CSRF_COOKIE_NAME,
            csrf_token,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            max_age=43200,
            path="/",
        )
        return response

    @app.post("/settings")
    def update_settings(
        request: Request,
        csrf_token: str = Form(""),
        api_key: str = Form(""),
        profile_path: str = Form(""),
        resume_path: str = Form(""),
        application_phone: str = Form(""),
        application_referred: str = Form(""),
        application_referral_details: str = Form(""),
        application_visa_sponsorship: str = Form(""),
        application_relocation: str = Form(""),
    ):
        validate_csrf_token(request, csrf_token)
        save_configuration(
            api_key,
            profile_path,
            resume_path,
            application_phone,
            application_referred,
            application_referral_details,
            application_visa_sponsorship,
            application_relocation,
        )
        state.set_message("Local settings and reusable application answers saved.")
        return RedirectResponse(url="/", status_code=303)

    @app.post("/jobs/import")
    def import_jobs(
        request: Request,
        csrf_token: str = Form(""),
        country: str = Form("India"), keyword: str = Form(""), remote_only: str = Form("false"),
        company_size: str = Form(""), employment_type: str = Form(""), limit: str = Form("30"),
    ):
        validate_csrf_token(request, csrf_token)
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

    @app.post("/applications/submit")
    def submit_applications(
        request: Request,
        csrf_token: str = Form(""),
        job_ids: list[str] = Form(default=[]),
    ):
        validate_csrf_token(request, csrf_token)
        config = load_configuration()
        errors = configuration_errors(config)
        if errors:
            state.set_message(" ".join(errors), error=True)
            return RedirectResponse(url="/", status_code=303)

        selected = set(job_ids)
        with state.lock:
            if state.is_running:
                state.message = "Another import or application submission is already in progress."
                return RedirectResponse(url="/", status_code=303)
            selected_jobs = [
                job for job in state.jobs
                if job["id"] in selected and job["link"] not in state.submitted_job_links
            ]
            if not selected_jobs:
                state.message = "Select at least one job before submitting applications."
                state.error = True
                return RedirectResponse(url="/", status_code=303)
            state.is_running = True
            state.error = False
            state.message = (
                f"Generating cover letters and submitting {len(selected_jobs)} application(s)…"
            )
        threading.Thread(
            target=submit_applications_in_background,
            args=(config, selected_jobs),
            daemon=True,
        ).start()
        return RedirectResponse(url="/", status_code=303)

    return app


def run() -> None:
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=8000)
