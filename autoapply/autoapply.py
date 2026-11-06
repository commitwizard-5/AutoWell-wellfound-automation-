import re
from collections.abc import Callable
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.firefox.service import Service as FirefoxService
from autoapply.helper.gotolink import Gotolink
from autoapply.helper.firefox_driver import resolve_geckodriver_path


class JobScraper:

    JOB_PATH = re.compile(r"^/jobs/\d+(?:-[^/]+)?$")

    def __init__(self, profile_path, progress_callback: Callable[[str], None] | None = None):
        self.options = FirefoxOptions()

        self.options.add_argument("-profile")
        self.options.add_argument(profile_path)

        self.driver = webdriver.Firefox(
            service=FirefoxService(executable_path=resolve_geckodriver_path()),
            options=self.options
        )

        self.driver.get('https://wellfound.com/jobs')

        self.data = []
        self.progress_callback = progress_callback
        self.scrape_warning = ""


    def _report(self, message: str) -> None:
        if self.progress_callback:
            self.progress_callback(message)

    def _job_links(self, maximum_jobs: int = 0) -> list[tuple[str, str, str]]:
        """Find job URLs and visible card text without relying on hashed CSS classes."""
        try:
            WebDriverWait(self.driver, 15).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, "a[href*='/jobs/']"))
            )
        except TimeoutException as error:
            raise RuntimeError(
                "Wellfound did not expose job links. Confirm that Firefox is signed in and the jobs page loaded."
            ) from error

        links, seen = [], set()
        for _ in range(5):
            elements = self.driver.find_elements(By.CSS_SELECTOR, "a[href*='/jobs/']")
            for element in elements:
                href = element.get_attribute("href")
                if not href:
                    continue
                parsed = urlparse(href)
                if (
                    (parsed.netloc == "wellfound.com" or parsed.netloc.endswith(".wellfound.com"))
                    and self.JOB_PATH.fullmatch(parsed.path)
                    and href not in seen
                ):
                    seen.add(href)
                    title = element.text.strip()
                    card_text = self.driver.execute_script(
                        """
                        let node = arguments[0];
                        for (let depth = 0; node && depth < 6; depth++, node = node.parentElement) {
                          const text = (node.innerText || "").trim();
                          if (text.length >= 30 && text.length <= 1800) return text;
                        }
                        return arguments[0].innerText || "";
                        """,
                        element,
                    )
                    links.append((href, title, card_text))
                    if maximum_jobs and len(links) >= maximum_jobs:
                        break
            if not maximum_jobs or len(links) >= maximum_jobs:
                break
            previous_count = len(elements)
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            try:
                WebDriverWait(self.driver, 2).until(
                    lambda driver: len(
                        driver.find_elements(By.CSS_SELECTOR, "a[href*='/jobs/']")
                    ) > previous_count
                )
            except TimeoutException:
                break
        if not links:
            raise RuntimeError("Found the jobs page, but no individual job listings were available.")
        return links

    @staticmethod
    def _normalized_text(value: str) -> str:
        return " ".join(value.casefold().split())

    def _visible_named_control(
        self,
        labels: tuple[str, ...],
        selectors: tuple[str, ...] = ("button", "[role='button']"),
    ):
        wanted = tuple(self._normalized_text(label) for label in labels)
        for selector in selectors:
            for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if not element.is_displayed():
                        continue
                    text = self._normalized_text(element.text)
                    aria_label = self._normalized_text(element.get_attribute("aria-label") or "")
                    if text in wanted or aria_label in wanted:
                        return element
                except StaleElementReferenceException:
                    continue
        return None

    def _select_search_value(self, button_labels: tuple[str, ...], value: str, field_name: str) -> None:
        if self._visible_named_control((value,)):
            return
        button = self._visible_named_control(button_labels)
        if button is None:
            raise RuntimeError(f"Wellfound's {field_name} search control was not found.")
        button.click()

        def find_search_input(driver):
            for selector in ("input[aria-autocomplete='list']", "input[id^='react-select-']", "[role='combobox']"):
                for element in driver.find_elements(By.CSS_SELECTOR, selector):
                    try:
                        if element.is_displayed():
                            return element
                    except StaleElementReferenceException:
                        continue
            return False

        field = WebDriverWait(self.driver, 8).until(find_search_input)
        self.driver.execute_script("arguments[0].focus();", field)
        field.send_keys(Keys.CONTROL, "a")
        field.send_keys(value)
        try:
            option = WebDriverWait(self.driver, 5).until(
                lambda driver: self._visible_named_control(
                    (value,),
                    (
                        "[role='option']",
                        "[id*='react-select-'][id*='option']",
                        "button",
                        "[role='button']",
                    ),
                )
            )
            option.click()
        except TimeoutException as error:
            raise RuntimeError(
                f"Wellfound did not show an exact {field_name} option for {value!r}; "
                "left the search value unselected."
            ) from error

    def _apply_search_criteria(self, filters: dict[str, str], warnings: list[str]) -> None:
        criteria = (
            ("country", ("Add a location",), "location"),
            ("keyword", ("Add a job title", "Add a role"), "job title"),
        )
        for key, buttons, label in criteria:
            value = filters.get(key, "").strip()
            if not value:
                continue
            try:
                self._select_search_value(buttons, value, label)
            except Exception as error:
                detail = str(error).splitlines()[0].strip()
                warnings.append(f"Could not apply {label} search {value!r}: {detail}")

    def _open_filter_panel(self) -> bool:
        button = self._visible_named_control(("Filter", "Filters"), ("button", "[role='button']", "a"))
        if button is None:
            return False
        button.click()
        try:
            WebDriverWait(self.driver, 5).until(
                lambda driver: any(
                    element.is_displayed()
                    for element in driver.find_elements(
                        By.CSS_SELECTOR, "[role='dialog'], input[type='checkbox'], [role='checkbox'], [role='switch']"
                    )
                )
            )
            return True
        except TimeoutException:
            return False

    def _filter_control(self, label: str):
        wanted = self._normalized_text(label)
        for selector in (
            "label", "[role='checkbox']", "[role='switch']", "[role='option']",
            "button", "[role='button']", "div", "span",
        ):
            for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if element.is_displayed() and self._normalized_text(element.text) == wanted:
                        self.driver.execute_script(
                            "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});",
                            element,
                        )
                        return element
                except StaleElementReferenceException:
                    continue
        return None

    def _filter_toggle_state(self, element) -> bool | None:
        return self.driver.execute_script(
            """
            const node = arguments[0];
            const control = node.matches('input[type="checkbox"], input[type="radio"], [role="checkbox"], [role="switch"]')
              ? node
              : node.closest('label, [role="checkbox"], [role="switch"], button, [role="button"]');
            if (!control) return null;
            if (control.matches('input[type="checkbox"], input[type="radio"]')) return control.checked;
            if (control.control && ['checkbox', 'radio'].includes(control.control.type)) {
              return control.control.checked;
            }
            const input = control.querySelector('input[type="checkbox"], input[type="radio"]');
            if (input) return input.checked;
            const checked = control.getAttribute('aria-checked') || control.getAttribute('aria-pressed')
              || control.getAttribute('data-state');
            if (checked !== null) return ['true', 'checked', 'on'].includes(checked.toLowerCase());
            const label = node.closest('label');
            if (label && label.control && ['checkbox', 'radio'].includes(label.control.type)) {
              return label.control.checked;
            }
            if (label) {
              const labelInput = label.querySelector('input[type="checkbox"], input[type="radio"]');
              if (labelInput) return labelInput.checked;
            }
            let parent = control.parentElement;
            for (let depth = 0; parent && depth < 3; depth++, parent = parent.parentElement) {
              const parentState = parent.getAttribute('aria-checked') || parent.getAttribute('aria-pressed')
                || parent.getAttribute('data-state');
              if (parentState !== null) {
                return ['true', 'checked', 'on'].includes(parentState.toLowerCase());
              }
              const inputs = parent.querySelectorAll('input[type="checkbox"], input[type="radio"]');
              if (inputs.length === 1) return inputs[0].checked;
            }
            return null;
            """,
            element,
        )

    def _apply_filter_option(self, section: str, label: str, warnings: list[str]) -> None:
        control = self._filter_control(label)
        if control is None:
            heading = self._filter_control(section)
            if heading is not None:
                heading.click()
                control = self._filter_control(label)
        if control is None:
            warnings.append(f"Wellfound did not expose the {label!r} filter; skipped it.")
            return
        current = self._filter_toggle_state(control)
        if current is None:
            warnings.append(f"Could not verify the {label!r} filter state; skipped it.")
            return
        if not current:
            control.click()
            control = self._filter_control(label)
            current = self._filter_toggle_state(control) if control is not None else None
        if current is not True:
            warnings.append(f"Wellfound did not confirm the {label!r} filter; skipped it.")

    def _apply_wellfound_filters(self, filters: dict[str, str], warnings: list[str]) -> None:
        size_labels = {
            "1-10": "1-10 employees",
            "11-50": "11-50 employees",
            "51-200": "51-200 employees",
            "201-500": "201-500 employees",
            "501-1000": "501-1000 employees",
            "1001-5000": "1001-5000 employees",
            "5001+": "5000+ employees",
        }
        selected_size = filters.get("company_size", "")
        selected_type = filters.get("employment_type", "").strip()
        if not selected_size and not selected_type and filters.get("remote_only") != "true":
            return
        if not self._open_filter_panel():
            warnings.append("Wellfound's Filters panel was unavailable; skipped company size, job type, and remote filters.")
            return

        def apply_option(section: str, label: str) -> None:
            try:
                self._apply_filter_option(section, label, warnings)
            except Exception as error:
                warnings.append(f"Could not apply Wellfound filter {label!r}: {error}")

        if selected_type:
            type_labels = {
                "Full-time": "Full Time",
                "Part-time": "Part Time",
                "Internship": "Internship",
            }
            apply_option(
                "Job Types",
                type_labels.get(selected_type, selected_type),
            )
        if selected_size:
            size_label = size_labels.get(selected_size)
            if size_label:
                apply_option("Company size", size_label)
            else:
                warnings.append(f"Unsupported company size {selected_size!r}; skipped it.")
        if filters.get("remote_only") == "true":
            apply_option(
                "Remote culture",
                "Only show jobs at companies that are mostly or fully remote",
            )

        try:
            results_button = self._visible_named_control(
                ("View results", "Apply", "Apply filters", "Show jobs", "Show results"),
                ("button", "[role='button']"),
            )
            if results_button is not None:
                results_button.click()
            else:
                warnings.append("Wellfound did not show a results button after filtering; continuing with the displayed results.")
        except Exception as error:
            warnings.append(f"Could not submit the Wellfound filters: {error}")

    def _listing_jobs(self, maximum_jobs: int) -> list[dict[str, str]]:
        """Collect visible result cards only; do not open job pages during import."""
        listings = self._job_links(maximum_jobs)
        collected = []
        for link, title, card_text in listings[:maximum_jobs]:
            lines = [line.strip() for line in card_text.splitlines() if line.strip()]
            role = title or (lines[0] if lines else "Untitled role")
            company = next(
                (line for line in lines if line != role and len(line) < 100),
                "Company unavailable",
            )
            location = next(
                (
                    line for line in lines
                    if re.search(r"\b(remote|hybrid|on[- ]?site|office)\b", line, re.IGNORECASE)
                ),
                "Location unavailable",
            )
            compensation = next(
                (line for line in lines if any(symbol in line for symbol in ("$", "₹", "€", "£"))),
                "Compensation unavailable",
            )
            collected.append({
                "company": company,
                "role": role,
                "location": location,
                "stipend": compensation,
                "description": card_text or "Open the listing to review its description.",
                "link": link,
            })
        return collected

    def scrape_jobs(self, maximum_jobs: int = 30, filters: dict[str, str] | None = None):
        """Apply filters on Wellfound and collect result cards without opening job pages."""
        filters = filters or {}
        self.scrape_warning = ""
        warnings: list[str] = []
        self.driver.get("https://wellfound.com/jobs")
        self._apply_search_criteria(filters, warnings)
        self._apply_wellfound_filters(filters, warnings)
        self._report("Collecting filtered Wellfound job cards…")
        self.data = self._listing_jobs(maximum_jobs)
        if not self.data:
            raise RuntimeError("Wellfound returned no job listings for the selected search.")
        if warnings:
            self.scrape_warning = " Some Wellfound filters could not be applied: " + " ".join(warnings)

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
