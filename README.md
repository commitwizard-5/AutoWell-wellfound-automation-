# Autoply — Wellfound Job Application Assistant

Autoply is a local-first AI job application workflow assistant. It collects Wellfound job listings, lets you review roles in a browser dashboard, and drafts tailored cover letters from your résumé using Google Gemini.

## Features

- Collects and filters Wellfound job listings.
- Provides a browser-based dashboard to review roles.
- Generates tailored cover-letter drafts using Gemini.
- Records job applications in `job_applications.xlsx`.
- Supports configurable job filters and application settings.
- Keeps the dashboard bound to your local computer by default.

## Setup

1. Install Python 3.11 or newer.
2. Create and activate a virtual environment.
3. Install dependencies:

   ```bash
   pip install -e .
   ```

4. Copy `.env.example` to `.env`.
5. Configure your Gemini API key, Firefox profile path, and résumé path in `.env`.
6. Start the application:

   ```bash
   python main.py
   ```

7. Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

## How It Works

The job importer opens Wellfound's main jobs page, keeps an already-selected location, and uses the page's job-title picker for keyword searches. It opens the Filters panel only when a job type, company size, or remote-only option is requested. It does not call a separate Wellfound API. If a requested selection cannot be found or verified, the import reports an error instead of silently applying incomplete filters.

The dashboard uses a server-rendered Jinja template at `templates/index.html`. For the dashboard and its functionality to work correctly, run the application through `main.py`.

## Security and Privacy

The dashboard server binds to your local computer by default. However, when cover-letter generation is used, extracted résumé text, relevant links, and job descriptions are sent to the Google Gemini API using your API key.

Review Google's current terms and privacy practices before using this feature, and obtain the résumé owner's informed consent.

- Keep API keys and credentials in `.env`.
- Never commit `.env` or private résumé data to GitHub.
- Review generated cover letters before using them.
- Autoply drafts and logs applications by default.
- Set `AUTO_SUBMIT=true` only after carefully reviewing your configuration and selected roles.

Generated application records are stored in `job_applications.xlsx`.

## License

Add a license before distributing this project if you intend to make it open source.
