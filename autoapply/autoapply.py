import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.firefox import GeckoDriverManager
from selenium.webdriver.firefox.service import Service as FirefoxService
from autoapply.helper.gotolink import Gotolink


def _cached_geckodriver_path() -> str:
    """Resolve WebDriver without the Unix permission update that fails on Windows."""
    manager = GeckoDriverManager()
    return manager._get_driver_binary_path(manager.driver)


@dataclass
class AutomationProfile:
    """Disposable copy of a Firefox profile used exclusively by WebDriver."""

    path: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def create_automation_profile(
    source: Path,
    *,
    temporary_root: Path | None = None,
) -> AutomationProfile:
    """Copy browser session data without files that lock Firefox profiles."""
    if not source.is_dir():
        raise ValueError(f"Firefox profile directory was not found: {source}")

    destination = Path(
        tempfile.mkdtemp(
            prefix="autoply-firefox-",
            dir=str(temporary_root) if temporary_root else None,
        )
    )
    try:
        shutil.copytree(
            source,
            destination,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                "parent.lock",
                ".parentlock",
                "lock",
                "storage",
                "cache2",
                "startupCache",
                "thumbnails",
                "shader-cache",
                "datareporting",
            ),
        )
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return AutomationProfile(path=destination)


class JobScraper:

    JOB_PATH = re.compile(r"^/jobs/\d+(?:-[^/]+)?$")

    def __init__(self, profile_path, progress_callback: Callable[[str], None] | None = None):
        self.options = FirefoxOptions()
        self._automation_profile = create_automation_profile(Path(profile_path))

        self.options.add_argument("-profile")
        self.options.add_argument(str(self._automation_profile.path))

        try:
            self.driver = webdriver.Firefox(
                service=FirefoxService(
                    executable_path=_cached_geckodriver_path()
                ),
                options=self.options
            )
        except Exception:
            self._automation_profile.cleanup()
            raise

        self.driver.get('https://wellfound.com/jobs')

        self.data = []
        self.progress_callback = progress_callback
        self.scrape_warning = ""


    def _report(self, message: str) -> None:
        if self.progress_callback:
            self.progress_callback(message)

    def _search_input(
        self,
        field_name: str,
        aliases: tuple[str, ...],
        *,
        include_buttons: bool = False,
    ):
        selectors = [
            "input:not([type='hidden'])",
            "textarea",
            "[role='textbox']",
            "[role='combobox']",
            "[contenteditable='true']",
        ]
        if include_buttons:
            selectors.extend(("button", "[role='button']"))
        elements = []
        for selector in selectors:
            elements.extend(self.driver.find_elements(By.CSS_SELECTOR, selector))

        visible_descriptions = []
        direct_matches = []
        context_matches = []
        matching_terms = tuple(alias.casefold() for alias in aliases)
        seen = set()
        seen_elements = set()
        for element in elements:
            element_id = getattr(element, "id", None)
            if element_id is not None and element_id in seen_elements:
                continue
            if element_id is not None:
                seen_elements.add(element_id)
            if not element.is_displayed():
                continue
            labels = self.driver.execute_script(
                """
                const node = arguments[0];
                const labels = node.labels ? Array.from(node.labels).map(label => label.innerText) : [];
                const labelledBy = (node.getAttribute("aria-labelledby") || "")
                  .split(/\\s+/).map(id => document.getElementById(id)?.innerText || "");
                const parentLabel = node.closest("label");
                if (parentLabel) labels.push(parentLabel.innerText);
                return {
                  direct: [
                  node.getAttribute("aria-label"),
                  node.getAttribute("placeholder"),
                  node.getAttribute("name"),
                  node.getAttribute("id"),
                  node.getAttribute("data-testid"),
                  node.innerText,
                  ...labelledBy,
                  ...labels,
                  ].filter(Boolean).join(" ").replace(/\\s+/g, " ").trim(),
                  context: (node.parentElement?.innerText || "").replace(/\\s+/g, " ").trim()
                };
                """,
                element,
            )
            description = " ".join(str(labels.get("direct") or "").split())
            context = " ".join(str(labels.get("context") or "").split())
            if description and description not in seen:
                visible_descriptions.append(description[:140])
                seen.add(description)
            normalized = description.casefold()
            if any(alias in normalized for alias in matching_terms):
                direct_matches.append((element, normalized))
            context_normalized = context.casefold()
            if (
                len(context) <= 100
                and any(context_normalized == alias or context_normalized.startswith(f"{alias} ") for alias in matching_terms)
            ):
                context_matches.append(element)
        if direct_matches:
            exact = [
                element for element, description in direct_matches
                if any(description == alias for alias in matching_terms)
            ]
            return exact[0] if exact else direct_matches[0][0]
        if len(context_matches) == 1:
            return context_matches[0]
        available = "; ".join(visible_descriptions[:8]) or "no visible text-entry controls"
        if len(context_matches) > 1:
            available = f"{available}; multiple fields have matching nearby labels"
        raise RuntimeError(
            f"Wellfound {field_name} search field was not found. "
            f"Visible fields: {available}"
        )

    def _wait_for_search_input(
        self,
        field_name: str,
        aliases: tuple[str, ...],
        *,
        include_buttons: bool = False,
    ):
        """Wait for Wellfound's client-rendered search control to become available."""
        last_error: RuntimeError | None = None

        def find_input(_driver):
            nonlocal last_error
            try:
                return self._search_input(
                    field_name,
                    aliases,
                    include_buttons=include_buttons,
                )
            except RuntimeError as error:
                last_error = error
                return False

        try:
            return WebDriverWait(self.driver, 15).until(find_input)
        except TimeoutException as error:
            detail = str(last_error) if last_error else "no matching control became visible"
            raise RuntimeError(
                f"Wellfound {field_name} search field was not available after the jobs page loaded. {detail}"
            ) from error

    @staticmethod
    def _fill_search_field(element, value: str) -> None:
        element.click()
        element.send_keys(Keys.CONTROL, "a")
        element.send_keys(Keys.BACKSPACE)
        if value:
            element.send_keys(value)

    def _has_visible_button(self, label: str) -> bool:
        normalized_label = " ".join(label.casefold().split())
        for selector in ("button", "[role='button']"):
            for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if (
                        element.is_displayed()
                        and " ".join(element.text.casefold().split()) == normalized_label
                    ):
                        return True
                except StaleElementReferenceException:
                    continue
        return False

    def _click_search_button(self, label: str) -> None:
        normalized_label = " ".join(label.casefold().split())

        def click_button(_driver):
            for selector in ("button", "[role='button']"):
                for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    try:
                        if (
                            element.is_displayed()
                            and " ".join(element.text.casefold().split()) == normalized_label
                        ):
                            element.click()
                            return True
                    except StaleElementReferenceException:
                        continue
            return False

        try:
            WebDriverWait(self.driver, 15).until(click_button)
        except TimeoutException as error:
            raise RuntimeError(f"Wellfound's {label!r} search control was not found.") from error

    def _select_search_value(self, button_label: str, value: str, field_name: str) -> None:
        self._click_search_button(button_label)

        def find_autocomplete(driver):
            for selector in ("input[id^='react-select-']", "input[aria-autocomplete='list']"):
                for element in driver.find_elements(By.CSS_SELECTOR, selector):
                    try:
                        if element.is_displayed():
                            return element
                    except StaleElementReferenceException:
                        continue
            return False

        try:
            field = WebDriverWait(self.driver, 15).until(find_autocomplete)
        except TimeoutException as error:
            raise RuntimeError(
                f"Wellfound did not open the {field_name} selector after clicking {button_label!r}."
            ) from error

        self._fill_search_field(field, value)
        field.send_keys(Keys.ARROWDOWN, Keys.ENTER)
        try:
            WebDriverWait(self.driver, 10).until(lambda _driver: self._has_visible_button(value))
        except TimeoutException as error:
            raise RuntimeError(
                f"Wellfound did not confirm {value!r} as the selected {field_name}."
            ) from error

    def _set_location(self, country: str) -> None:
        if self._has_visible_button(country):
            return
        if not self._has_visible_button("Add a location"):
            raise RuntimeError(
                f"Wellfound's location selector is not available for {country!r}. "
                "Choose a location on the jobs page and try again."
            )
        self._select_search_value("Add a location", country, "location")
        current_path = urlparse(self.driver.current_url).path.rstrip("/")
        if current_path != "/jobs":
            raise RuntimeError(
                "Wellfound redirected away from the main jobs page after setting "
                "the location. No filters were applied; return to the jobs page and try again."
            )

    def _apply_job_title(self, keyword: str) -> None:
        if not keyword or self._has_visible_button(keyword):
            return
        self._select_search_value("Add a job title", keyword, "job-title")

    def _open_filter_panel(self) -> None:
        def find_filter_button(_driver):
            for selector in ("button", "[role='button']", "a"):
                for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    if not element.is_displayed():
                        continue
                    label = " ".join(
                        " ".join(
                            str(element.get_attribute(attribute) or "")
                            for attribute in ("aria-label", "title")
                        ).casefold().split()
                    )
                    text = " ".join(element.text.casefold().split())
                    if text in ("filter", "filters") or label in ("filter", "filters"):
                        return element
            return False

        try:
            filter_button = WebDriverWait(self.driver, 15).until(find_filter_button)
        except TimeoutException as error:
            buttons = [
                " ".join(element.text.split())
                for element in self.driver.find_elements(By.CSS_SELECTOR, "button")
                if element.is_displayed() and element.text.strip()
            ]
            visible_buttons = ", ".join(buttons[:8]) or "none"
            raise RuntimeError(
                "Wellfound's Filters button was not found. "
                f"Current page: {self.driver.title!r} ({self.driver.current_url}); "
                f"visible buttons: {visible_buttons}. "
                "Check that Firefox is signed in and the jobs page loaded normally."
            ) from error

        filter_button.click()
        try:
            WebDriverWait(self.driver, 10).until(
                lambda driver: any(
                    element.is_displayed()
                    for element in driver.find_elements(By.CSS_SELECTOR, "input, button, [role='checkbox']")
                )
            )
        except TimeoutException as error:
            raise RuntimeError("Wellfound's Filters panel did not open.") from error

    def _submit_filter_panel(self) -> None:
        for selector in ("button", "[role='button']"):
            for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                if not element.is_displayed():
                    continue
                text = " ".join(element.text.casefold().split())
                if (
                    text in ("apply", "apply filters", "show jobs", "show results", "view jobs", "done")
                    or (text.startswith("show ") and ("job" in text or "result" in text))
                ):
                    element.click()
                    return
        raise RuntimeError("Wellfound's Filters panel did not expose an apply-results button.")

    def _search_wellfound(self, country: str, keyword: str) -> None:
        self.keyword_search_applied = False
        stage = "opening Wellfound jobs"
        self.driver.get("https://wellfound.com/jobs")
        try:
            if country:
                stage = "setting the country/location"
                self._set_location(country)
            if keyword:
                stage = "setting the job-title search"
                self._apply_job_title(keyword)
                self.keyword_search_applied = True
        except Exception as error:
            detail = str(error).strip() or type(error).__name__
            raise RuntimeError(
                f"Could not configure Wellfound's search filters while {stage}: {detail}"
            ) from error

        self._report("Set the Wellfound location and job-title search.")

    @staticmethod
    def _matches_job_title(title: str, keyword: str) -> bool:
        normalized_keyword = keyword.casefold().strip()
        return not normalized_keyword or normalized_keyword in title.casefold()

    def _toggle_state(self, element) -> bool | None:
        if element.tag_name == "input" and element.get_attribute("type") in ("checkbox", "radio"):
            return element.is_selected()
        associated_state = self.driver.execute_script(
            """
            const control = arguments[0].control;
            return control && (control.type === "checkbox" || control.type === "radio")
              ? control.checked
              : null;
            """,
            element,
        )
        if associated_state is not None:
            return bool(associated_state)
        if element.tag_name == "label":
            inputs = element.find_elements(By.CSS_SELECTOR, "input[type='checkbox'], input[type='radio']")
            if inputs:
                return inputs[0].is_selected()
        for attribute in ("aria-checked", "aria-pressed", "data-state"):
            value = element.get_attribute(attribute)
            if value is not None:
                return value.casefold() in ("true", "checked", "on")
        inputs = element.find_elements(By.CSS_SELECTOR, "input[type='checkbox'], input[type='radio']")
        if inputs:
            return inputs[0].is_selected()
        return None

    def _filter_control(self, label: str):
        normalized = " ".join(label.casefold().split())
        selectors = (
            "label",
            "[role='checkbox']",
            "[role='switch']",
            "[role='option']",
            "button",
            "[role='button']",
            "[role='heading']",
            "h2",
            "h3",
            "summary",
            "div",
            "span",
        )
        for selector in selectors:
            for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                if element.is_displayed() and " ".join(element.text.casefold().split()) == normalized:
                    return element
        return None

    def _apply_filter_option(self, section: str, label: str, selected: bool = True) -> None:
        control = self._filter_control(label)
        if control is None:
            heading = self._filter_control(section)
            if heading is not None:
                heading.click()
            control = self._filter_control(label)
        if control is None:
            raise RuntimeError(f"Wellfound filter {label!r} was not found under {section!r}.")

        current = self._toggle_state(control)
        if current is None:
            raise RuntimeError(
                f"Wellfound exposed {label!r}, but its selected state could not be verified."
            )
        if current != selected:
            control.click()
            control = self._filter_control(label)
            if control is None:
                raise RuntimeError(f"Wellfound filter {label!r} disappeared after selection.")
            current = self._toggle_state(control)
        if current != selected:
            raise RuntimeError(f"Wellfound did not apply the {label!r} filter.")

    def _set_filter_group(self, section: str, labels: list[str], selected_label: str = "") -> None:
        opened_section = False
        for label in labels:
            control = self._filter_control(label)
            if control is None and not opened_section:
                heading = self._filter_control(section)
                if heading is not None:
                    heading.click()
                    opened_section = True
            control = self._filter_control(label)
            if control is None:
                raise RuntimeError(f"Wellfound filter {label!r} was not found under {section!r}.")
            current = self._toggle_state(control)
            if current is None:
                raise RuntimeError(
                    f"Wellfound exposed {label!r}, but its selected state could not be verified."
                )
            should_select = label == selected_label
            if current != should_select:
                control.click()
                control = self._filter_control(label)
                if control is None:
                    raise RuntimeError(f"Wellfound filter {label!r} disappeared after selection.")
                current = self._toggle_state(control)
            if current != should_select:
                raise RuntimeError(f"Wellfound did not update the {label!r} filter.")

    def _apply_wellfound_filters(self, filters: dict[str, str]) -> None:
        employment_types = {
            "Full-time": "Full Time",
            "Contract": "Contract",
            "Internship": "Internship",
            "Cofounder": "Cofounder",
        }
        selected_type = filters.get("employment_type", "")
        if selected_type and selected_type not in employment_types:
            raise RuntimeError(
                f"{selected_type!r} is not an employment type available on Wellfound. "
                "Choose Full Time, Contract, Internship, or Cofounder."
            )
        company_size = filters.get("company_size", "")
        size_labels = {
            "1-10": "1-10 employees",
            "11-50": "11-50 employees",
            "51-200": "51-200 employees",
            "201-500": "201-500 employees",
            "501-1000": "501-1000 employees",
            "1001-5000": "1001-5000 employees",
            "5000+": "5000+ employees",
        }
        if company_size and company_size not in size_labels:
            raise RuntimeError(f"Unsupported Wellfound company size: {company_size!r}.")
        remote_only = filters.get("remote_only") == "true"
        if not selected_type and not company_size and not remote_only:
            return

        self._open_filter_panel()
        if selected_type:
            if self._filter_control("Full Time") is None and self._filter_control("Job Types") is None:
                job_details = self._filter_control("Job Details")
                if job_details is not None:
                    job_details.click()
            self._set_filter_group(
                "Job Types",
                list(employment_types.values()),
                employment_types[selected_type],
            )
        if company_size:
            self._set_filter_group(
                "Company size",
                list(size_labels.values()),
                size_labels[company_size],
            )
        if remote_only:
            self._apply_filter_option(
                "Remote culture",
                "Only show jobs at companies that are mostly or fully remote",
            )
        self._submit_filter_panel()

    def _job_links(self) -> list[tuple[str, str]]:
        """Find job URLs and their own visible link text."""
        try:
            WebDriverWait(self.driver, 15).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, "a[href*='/jobs/']"))
            )
        except TimeoutException as error:
            raise RuntimeError(
                "Wellfound did not expose job links. Confirm that Firefox is signed in and the jobs page loaded."
            ) from error

        links, seen = [], set()
        for element in self.driver.find_elements(By.CSS_SELECTOR, "a[href*='/jobs/']"):
            href = element.get_attribute("href")
            if not href:
                continue
            parsed = urlparse(href)
            if parsed.netloc.endswith("wellfound.com") and self.JOB_PATH.fullmatch(parsed.path) and href not in seen:
                seen.add(href)
                title = element.text.strip()
                links.append((href, title))
        if not links:
            raise RuntimeError("Found the jobs page, but no individual job listings were available.")
        return links

    def _job_details(self, link: str) -> dict[str, str]:
        self.driver.get(link)
        title = WebDriverWait(self.driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "h1"))
        ).text.strip()
        page_text = self.driver.find_element(By.TAG_NAME, "body").text
        company = "Company unavailable"
        company_links = self.driver.find_elements(By.CSS_SELECTOR, "a[href*='/company/']")
        if company_links and company_links[0].text.strip():
            company = company_links[0].text.strip()

        description = page_text.split("About the job", 1)[-1]
        description = description.split("About the company", 1)[0]
        description = " ".join(description.split())
        header = page_text.split("About the job", 1)[0]
        location, compensation = "Location unavailable", "Compensation unavailable"
        for line in (line.strip("•| ") for line in header.splitlines()):
            if any(symbol in line for symbol in ("$", "₹", "€", "£")):
                compensation = line
            if "remote" in line.lower() or "office" in line.lower():
                location = line

        employee_match = re.search(
            r"\b(1-10|11-50|51-200|201-500|501-1000|1001-5000|5001\+)\s+Employees\b",
            header,
            re.IGNORECASE,
        )
        return {"company": company, "role": title or "Untitled role", "location": location,
                "stipend": compensation, "description": description or "Description unavailable.",
                "employee_count": employee_match.group(1) if employee_match else "Not listed",
                "page_text": page_text, "link": link}

    def scrape_jobs(self, maximum_jobs: int = 30, filters: dict[str, str] | None = None):
        """Import a bounded set of roles and their descriptions for dashboard review."""
        filters = filters or {}
        self._search_wellfound(filters.get("country", ""), filters.get("keyword", ""))
        self._apply_wellfound_filters(filters)
        try:
            WebDriverWait(self.driver, 20).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, "a[href*='/jobs/']"))
            )
        except TimeoutException as error:
            raise RuntimeError(
                "Wellfound did not show job results after applying the selected filters."
            ) from error
        candidate_limit = min(max(maximum_jobs * 5, 30), 100)
        listings = self._job_links()[:candidate_limit]
        keyword = filters.get("keyword", "")
        candidates = [
            (link, title) for link, title in listings
            if self.keyword_search_applied or not title or self._matches_job_title(title, keyword)
        ]
        self._report(
            f"Wellfound filters are set. Checking {len(candidates)} candidate listings…"
        )
        self.data = []
        failures = []
        filtered_out = len(listings) - len(candidates)
        for index, (link, _title) in enumerate(candidates, start=1):
            self._report(f"Checking job {index} of {len(candidates)} ({len(self.data)} imported)…")
            try:
                job = self._job_details(link)
                if not self._matches_job_title(job["role"], keyword):
                    filtered_out += 1
                    continue
                job.pop("page_text", None)
                self.data.append(job)
                if len(self.data) >= maximum_jobs:
                    break
            except Exception as error:
                failures.append(f"{type(error).__name__}: {error}")
        if not self.data:
            if failures:
                details = "; ".join(failures[:3])
                raise RuntimeError(
                    f"No jobs were imported. {len(failures)} listing page(s) failed to load. "
                    f"First error(s): {details}"
                )
            if filtered_out:
                raise RuntimeError(
                    f"No jobs matched the selected filters ({filtered_out} listing(s) checked). "
                    "Broaden the filters and try again."
                )
            raise RuntimeError("Wellfound returned no job listings for the selected filters.")
        if failures:
            self.scrape_warning = f" Skipped {len(failures)} listing page(s) that could not be read."

    def run_gui(self):
        raise RuntimeError("The Tkinter selector was replaced by the local web dashboard.")

    def process_links(self):
        # Firefox cannot reliably open the same profile in two live sessions.
        self.close()
        gotolink = Gotolink()
        gotolink.process_links(d=self.data)

    def close(self):
        if getattr(self, "driver", None):
            try:
                self.driver.quit()
            except Exception:
                pass
            finally:
                self.driver = None
        automation_profile = getattr(self, "_automation_profile", None)
        if automation_profile is not None:
            automation_profile.cleanup()
            self._automation_profile = None
