# AutoWell

<p align="center">
  <strong>A more thoughtful way through the busywork of a job search.</strong><br>
  Discover Wellfound opportunities, prepare grounded applications, and keep the candidate in control.
</p>

<p align="center">
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python 3.11 or newer"></a>
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/FastAPI-local%20web%20app-009688?logo=fastapi&logoColor=white" alt="FastAPI"></a>
  <img src="https://img.shields.io/badge/Browser-Firefox%20%2B%20Selenium-FF7139?logo=firefoxbrowser&logoColor=white" alt="Firefox and Selenium">
  <img src="https://img.shields.io/badge/AI-Gemini%20%2B%20optional%20DeepSeek-8B5CF6" alt="Gemini with optional DeepSeek fallback">
</p>

## The experience

The project opens with a cinematic mountain landscape, then turns the scroll into a guided tour of the real application: source-code chapters, the job-search workflow, an interactive sample dashboard, verified capabilities, the system architecture, project milestones, and a final workspace link.

The scrolling preview below is an animated capture of the landing page—not a separate mockup:

<p align="center">
  <img src="docs/landing-scroll.gif" alt="Animated walkthrough scrolling through the AutoWell landing page, from its mountain hero through product and architecture sections" width="800">
</p>

The landing page and application dashboard are both served by the existing FastAPI app:

- **Showcase:** [http://127.0.0.1:8000/](http://127.0.0.1:8000/)
- **Working dashboard:** [http://127.0.0.1:8000/dashboard](http://127.0.0.1:8000/dashboard)

Start the app with `python main.py`; see [Setup](#setup).

## Architecture

<p align="center">
  <a href="docs/autowell-architecture.html"><strong>Open the interactive AutoWell architecture diagram ↗</strong></a><br>
  <sub>Standalone HTML with pan, zoom, light/dark themes, and animated relationships.</sub>
</p>

The interactive map follows the current repository: local FastAPI routes and background workers coordinate Wellfound discovery, Selenium/Firefox application handling, optional AI providers, local resume context, in-memory session state, and the Excel application log.

<p align="center">
  <img src="docs/autoply-architecture.jpg" alt="Original AutoPly system architecture illustration supplied with the project" width="800">
</p>

<sub>The supplied AutoPly illustration is retained as an earlier project sketch. Some labels and components describe a previous design, so the interactive diagram above is the code-backed view of the current application.</sub>

## How it works

1. Configure a signed-in Firefox profile and your candidate details in the local dashboard.
2. Filter and import visible job cards from Wellfound.
3. Select the opportunities you want to process.
4. AutoWell reads each role, prepares a cover letter, and inspects the application form.
5. Known answers can be reused; unsupported required questions stop for your review.
6. Application attempts and outcomes are recorded locally.

The application uses a configured Gemini provider first, optional DeepSeek next, and a built-in local cover-letter template if neither returns usable content. AI credentials are optional.

## Setup

1. Create and activate a Python 3.11+ virtual environment.
2. Install the project:

   ```bash
   pip install -e .
   ```

3. Copy `.env.example` to `.env`. Set the path to your signed-in Firefox profile. A resume PDF is optional but can provide relevant experience and links.
4. Optionally add `GEMINI_API_KEY` and/or `DEEPSEEK_API_KEY`. Without provider keys, cover letters use the built-in template.
5. Run the server and open the showcase:

   ```bash
   python main.py
   ```

   Visit [http://127.0.0.1:8000/](http://127.0.0.1:8000/) or open the working dashboard directly at [http://127.0.0.1:8000/dashboard](http://127.0.0.1:8000/dashboard).

AutoWell searches for geckodriver in `GECKODRIVER_PATH`, `PATH`, and its local driver caches before trying a download. To use a manually installed driver, set its full path in `.env`, for example `GECKODRIVER_PATH=C:/tools/geckodriver.exe`.

## Candidate control and data

- Applications are submitted immediately when the dashboard is in normal mode. Review selected jobs before starting; there is no pre-submission cover-letter preview.
- Set `AUTOWELL_DRY_RUN=true` in `.env` to inspect forms without AI calls, letter insertion, or application submission.
- Required questions without a supported, confirmed answer pause the workflow for review instead of inventing personal information.
- Dashboard settings and saved answers are stored in `.env`; the shortlist and progress use in-memory state; application records are stored in `job_applications.xlsx`.
- If an AI provider is used, resume text and links, saved answers, job descriptions, and application questions may be sent to that provider. Use only with the resume owner's consent and review the provider's data-handling terms.
- Keep `.env`, resume files, application records, and `form_debug/` diagnostics private. Diagnostics may contain personal data.

## Project layout

```text
autoapply/       FastAPI web routes, Wellfound importer, application workflow
templates/       Server-rendered showcase and working dashboard
static/          Landing-page interactions, styling, and local mountain image
docs/            Interactive architecture, scroll preview, supplied sketch
tests/           Application, provider fallback, driver, and route tests
```

## Tests

Run the test suite with:

```bash
python -m unittest discover -s tests -v
```
