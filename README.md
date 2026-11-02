# Autoply

Autoply is a local-first AI job application workflow assistant. It collects Wellfound job listings, lets you review roles in a browser dashboard, and will draft tailored cover letters from your résumé using Gemini.

## Setup

1. Create and activate a Python 3.11+ virtual environment.
2. Install dependencies with `pip install -e .`.
3. Copy `.env.example` to `.env` and set your Gemini API key, Firefox profile path, and résumé path.
4. Run `python main.py` and open [http://127.0.0.1:8000](http://127.0.0.1:8000).

`templates/index.html` is a server-rendered Jinja template, not a standalone page. Its stylesheet can load when the file is opened directly, but template values and dashboard functionality require running `main.py`.

The job importer stays on Wellfound's main jobs page, applies available search and filter controls there, and imports visible result cards without opening each job page. If a requested filter control is unavailable, the import continues and reports which filters could not be applied. It does not use a separate Wellfound API.

The dashboard server binds only to your computer, but that does not mean all data stays on your device. When an application is generated, extracted résumé text and links, saved reusable answers, the job description, and application questions are sent to the Google Gemini API using your API key. Gemini drafts role-specific answers using the résumé and your reusable answers from Local settings. Phone, referral, referral contact, visa sponsorship, and relocation answers are saved locally in `.env`; enter your real answers before submitting. If a required question cannot be answered using your résumé or saved profile answers, the application is not submitted. Autoply cannot guarantee Google's processing or retention practices; review Google's current terms and use this feature only with the résumé owner's informed consent. Keep your API key in `.env` and never commit that file. Application attempts, including failures, are recorded in `job_applications.xlsx` in the project directory. Selecting jobs and clicking **Generate & submit selected applications** submits them immediately without a cover-letter preview. Review the selected roles before clicking.
