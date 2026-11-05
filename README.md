# Autowell

Autowell is a local-first AI job application workflow assistant. It collects Wellfound job listings, lets you review roles in a browser dashboard, and generates tailored cover letters using configured AI providers or its built-in offline template.

## Setup

1. Create and activate a Python 3.11+ virtual environment.
2. Install dependencies with `pip install -e .`.
3. Copy `.env.example` to `.env`. Set your Firefox profile path; a resume path is recommended but optional. Gemini and DeepSeek API keys are optional; without either key Autowell generates cover letters locally from available job and resume information.
4. Run `python main.py` and open [http://127.0.0.1:8000](http://127.0.0.1:8000).

`templates/index.html` is a server-rendered Jinja template, not a standalone page. Its stylesheet can load when the file is opened directly, but template values and dashboard functionality require running `main.py`.

Dashboard forms use a random CSRF token checked against a `SameSite=Strict` cookie, helping prevent other websites from silently triggering local settings changes, job imports, or application submissions in an open browser session.

Cover letters use a three-level provider chain: Gemini (`GEMINI_API_KEY`, with legacy `API_KEY` support), optional DeepSeek (`DEEPSEEK_API_KEY`) through its OpenAI-compatible API, then a built-in offline template. Temporary provider failures receive a bounded retry; permanent errors and invalid responses move to the next provider. The template uses only available job/resume details and works without API keys or internet access. Application records include the provider used. Gemini/DeepSeek may also answer application questions; required questions without a supported answer still block submission.

The job importer stays on Wellfound's main jobs page, applies available search and filter controls there, and imports visible result cards without opening each job page. If a requested filter control is unavailable, the import continues and reports which filters could not be applied. It does not use a separate Wellfound API.

The dashboard server binds only to your computer, but that does not mean all data stays on your device. When an AI provider is used, extracted resume text and links, saved reusable answers, the job description, and application questions may be sent to that provider. Review the providers' current data-handling terms and use this feature only with the resume owner's informed consent. Phone, referral, referral contact, visa sponsorship, and relocation answers are saved locally in `.env`; enter your real answers before submitting. If a required question cannot be answered using your resume or saved profile answers, the application is not submitted. Keep API keys in `.env` and never commit that file. Application attempts, including failures and the cover-letter provider, are recorded in `job_applications.xlsx` in the project directory. Selecting jobs and clicking **Generate & submit selected applications** submits them immediately without a cover-letter preview. Review the selected roles before clicking.
