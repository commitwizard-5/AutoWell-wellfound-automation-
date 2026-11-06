import os
import re
import logging
from collections.abc import Callable
from pathlib import Path
from time import monotonic, sleep
from datetime import datetime

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions

from autoapply.helper.excel_handler import ExcelHandler
from autoapply.helper.application_answers import (
    is_sensitive_question,
    match_profile_answer,
    match_saved_question,
    normalize_question,
)
from autoapply.helper.genai import CoverLetterGenerator
from autoapply.helper.firefox_driver import resolve_geckodriver_path


load_dotenv()
LOGGER = logging.getLogger(__name__)


class Gotolink:
    def __init__(
        self,
        *,
        auto_submit=None,
        api_key=None,
        deepseek_api_key=None,
        resume_path=None,
        excel_path="job_applications.xlsx",
        progress_callback: Callable[[str], None] | None = None,
        profile_answers: dict[str, str] | None = None,
        question_answers: dict[str, dict[str, str]] | None = None,
        question_callback: Callable[[list[dict[str, str]]], None] | None = None,
        form_callback: Callable[[dict[str, object]], None] | None = None,
        dry_run: bool | None = None,
    ):
        profile_path = os.getenv("PROFILE_PATH")
        if not profile_path:
            raise ValueError("PROFILE_PATH is not configured.")

        self.options = FirefoxOptions()
        self.options.add_argument("-profile")
        self.options.add_argument(profile_path)
        self.excel_handler = ExcelHandler(excel_path)
        self.cover_letter_generator = CoverLetterGenerator(
            api_key=api_key,
            deepseek_api_key=deepseek_api_key,
            resume_path=resume_path,
            progress_callback=progress_callback,
        )
        self.progress_callback = progress_callback
        self.profile_answers = profile_answers or {}
        self.question_answers = question_answers or {}
        self.question_callback = question_callback
        self.form_callback = form_callback
        self.dry_run = (
            os.getenv("AUTOWELL_DRY_RUN", "false").strip().lower() == "true"
            if dry_run is None
            else dry_run
        )
        self.form_summary: dict[str, object] = {}
        self.pause_for_review = False
        self.generation_provider_counts: dict[str, int] = {}
        self.auto_submit = (
            os.getenv("AUTO_SUBMIT", "false").strip().lower() == "true"
            if auto_submit is None
            else auto_submit
        )
        self.driver = webdriver.Firefox(
            service=FirefoxService(executable_path=resolve_geckodriver_path()),
            options=self.options,
        )

    def _report(self, message: str) -> None:
        if message.startswith("[FORM]"):
            LOGGER.info("%s", message)
        if self.progress_callback:
            self.progress_callback(message)

    def _job_description(self) -> str:
        def find_description(driver):
            page_text = driver.find_element(By.TAG_NAME, "body").text
            match = re.search(
                r"About the job(.*?)(?:About the company|$)",
                page_text,
                re.IGNORECASE | re.DOTALL,
            )
            if match and match.group(1).strip():
                return " ".join(match.group(1).split())

            for selector in (
                "[data-test*='Description']",
                "[class*='description']",
                "article",
            ):
                for element in driver.find_elements(By.CSS_SELECTOR, selector):
                    text = " ".join(element.text.split())
                    if element.is_displayed() and len(text) > 80:
                        return text
            return False

        try:
            return WebDriverWait(self.driver, 15).until(find_description)
        except Exception as error:
            raise RuntimeError("The job page did not expose a readable job description.") from error

    def _click_apply(self) -> None:
        def find_apply_button(driver):
            for selector in (
                "[data-test='JobApplicationModal--ApplyButton']",
                "button[data-test*='Apply']",
                "button[class*='applyButton']",
                "button, [role='button']",
            ):
                for button in driver.find_elements(By.CSS_SELECTOR, selector):
                    if not button.is_displayed() or not button.is_enabled():
                        continue
                    if selector != "button, [role='button']":
                        return button
                    label = " ".join(button.text.casefold().split())
                    if re.fullmatch(r"apply(?: now| to this job)?", label):
                        return button
            return False

        try:
            WebDriverWait(self.driver, 15).until(find_apply_button).click()
        except Exception as error:
            raise RuntimeError("Could not find an available Apply button on this job page.") from error

    @staticmethod
    def _is_cover_letter_prompt(prompt: str) -> bool:
        normalized = normalize_question(prompt)
        return "cover letter" in normalized or "coverletter" in normalized

    def _cover_letter_field(self):
        for question in self._application_questions():
            if not question.get("cover_letter"):
                continue
            if question["type"] not in {"textarea", "text", "contenteditable"}:
                continue
            elements = self._application_field_elements(str(question["id"]))
            for element in elements:
                if self._field_is_interactable(element, str(question["id"])):
                    return element
        return None

    def _application_field_elements(self, question_id: str):
        return self.driver.execute_script(
            """
            const root = document.querySelector('[data-autowell-active-application="true"]');
            if (!root || !root.isConnected) return [];
            return Array.from(root.querySelectorAll('[data-autowell-question-id]'))
              .filter(node => node.getAttribute('data-autowell-question-id') === arguments[0]);
            """,
            question_id,
        ) or []

    def _field_is_interactable(self, element, question_id: str) -> bool:
        try:
            if not element.is_displayed() or not element.is_enabled():
                self._report(
                    f"[FORM] Excluded non-interactable field id={element.get_attribute('id')!r} "
                    f"name={element.get_attribute('name')!r} type={element.get_attribute('type')!r}."
                )
                return False
            belongs_to_form = self.driver.execute_script(
                """
                const element = arguments[0];
                const root = document.querySelector('[data-autowell-active-application="true"]');
                if (!root || !root.isConnected || !element.isConnected || !root.contains(element)) {
                  return false;
                }
                if (element.getAttribute('data-autowell-question-id') !== arguments[1]) {
                  return false;
                }
                const scroller = Array.from((function* () {
                  for (let node = element.parentElement; node && node !== root;
                       node = node.parentElement) yield node;
                })()).find(node => node.scrollHeight > node.clientHeight + 20
                  && /(auto|scroll|overlay)/.test(window.getComputedStyle(node).overflowY));
                if (scroller) {
                  const elementRect = element.getBoundingClientRect();
                  const scrollerRect = scroller.getBoundingClientRect();
                  if (elementRect.top < scrollerRect.top || elementRect.bottom > scrollerRect.bottom) {
                    scroller.scrollTop += elementRect.top < scrollerRect.top
                      ? elementRect.top - scrollerRect.top - 12
                      : elementRect.bottom - scrollerRect.bottom + 12;
                  }
                } else {
                  element.scrollIntoView({block: 'center', inline: 'nearest'});
                }
                return true;
                """,
                element,
                question_id,
            )
            return bool(belongs_to_form) and element.is_displayed() and element.is_enabled()
        except StaleElementReferenceException:
            return False

    def _log_field_interaction_failure(self, field, error: WebDriverException) -> None:
        details = []
        for attribute in ("id", "name", "type"):
            if field is None:
                details.append(f"{attribute}=<not found in active form>")
                continue
            try:
                details.append(f"{attribute}={field.get_attribute(attribute)!r}")
            except StaleElementReferenceException:
                details.append(f"{attribute}=<detached>")
        self._report(
            f"[FORM] Field interaction failed ({', '.join(details)}): "
            f"{type(error).__name__}; continuing with other fields."
        )
        self._save_application_form_debug_artifacts()

    def _insert_cover_letter_if_present(self, cover_letter: str) -> bool:
        field = self._cover_letter_field()
        if field is None:
            self._report("[FORM] No explicit cover-letter field found; skipping cover-letter insertion.")
            return False
        try:
            if not self._field_is_interactable(
                field, str(field.get_attribute("data-autowell-question-id"))
            ):
                return False
            field.clear()
            field.send_keys(cover_letter)
            current_value = self.driver.execute_script(
                "return arguments[0].value || arguments[0].innerText || '';",
                field,
            )
        except WebDriverException as error:
            self._report(
                f"[FORM] Could not insert the cover letter into its identified field "
                f"({type(error).__name__}); continuing without it."
            )
            self._save_application_form_debug_artifacts()
            return False
        if self._normalized_answer(str(current_value)) != self._normalized_answer(cover_letter):
            raise RuntimeError("The identified cover-letter field did not retain the generated letter.")
        self._report("[FORM] Cover letter inserted successfully.")
        return True

    def _wait_for_application_fields(self) -> list[dict[str, object]]:
        stable_count = 0
        last_count = -1
        stable_since = monotonic()
        scrolled_form = False

        def fields_ready(_driver):
            nonlocal stable_count, last_count, stable_since, scrolled_form
            fields = self._application_questions()
            count = len(fields)
            if count and not scrolled_form:
                self._scroll_application_container()
                fields = self._application_questions()
                count = len(fields)
                scrolled_form = True
            if count and count == last_count:
                stable_count += 1
            else:
                stable_count = 0
                stable_since = monotonic()
            last_count = count
            if count and stable_count >= 2 and monotonic() - stable_since >= 0.6:
                return fields
            return False

        try:
            fields = WebDriverWait(self.driver, 15, poll_frequency=0.25).until(fields_ready)
            counts = self._application_form_diagnostics().get("counts", {})
            self._report(
                "[FORM] Control counts: "
                f"inputs={counts.get('inputs', 0)}, "
                f"textareas={counts.get('textareas', 0)}, "
                f"radio groups={counts.get('radios', 0)}, "
                f"checkboxes={counts.get('checkboxes', 0)}, "
                f"dropdowns={counts.get('dropdowns', 0)}."
            )
            return fields
        except TimeoutException:
            fields = self._application_questions()
            if fields:
                return fields
            diagnostics = self._application_form_diagnostics()
            self._save_application_form_debug_artifacts()
            self._report(
                "[FORM] Detection diagnostics: "
                f"modal={diagnostics.get('modal_found')}, "
                f"raw controls={diagnostics.get('raw_controls', 0)}, "
                f"visible controls={diagnostics.get('visible_controls', 0)}, "
                f"types={diagnostics.get('counts', {})}."
            )
            if not diagnostics.get("modal_found") and not diagnostics.get("form_found"):
                reason = "The application modal did not appear in the page DOM."
            elif diagnostics.get("raw_controls", 0):
                reason = (
                    "The application modal is present, but its controls are hidden, "
                    "disabled, or unsupported by the field scanner."
                )
            else:
                reason = "The application modal is present but contains no form controls."
            raise RuntimeError(
                f"{reason} Local diagnostic screenshot and modal HTML were saved."
            )

    def _scroll_application_container(self) -> None:
        scroller = self.driver.execute_script(
            """
            const controlSelector = 'input:not([type="search"]):not(#search):not([name="search"]), '
              + 'textarea, select, [role="combobox"], '
              + '[role="textbox"], [role="radio"], [role="checkbox"], [contenteditable="true"]';
            const visible = node => {
              const style = window.getComputedStyle(node);
              return node.getClientRects().length > 0
                && style.visibility !== 'hidden' && style.display !== 'none';
            };
            const activeRoot = document.querySelector('[data-autowell-active-application="true"]');
            const candidates = activeRoot ? [activeRoot] : Array.from(document.querySelectorAll(
              '[role="dialog"], [aria-modal="true"], [data-test*="ApplicationModal"], '
              + '[data-testid*="ApplicationModal"], [class*="ApplicationModal"], form'
            )).filter(visible);
            const count = node => node.querySelectorAll(controlSelector).length;
            const applicationCandidates = [];
            for (const button of document.querySelectorAll('button, [role="button"]')) {
              if (!visible(button) || !/send application|submit application/i.test(button.innerText || '')) {
                continue;
              }
              for (let parent = button.parentElement, depth = 0;
                   parent && parent !== document.body && depth < 8;
                   parent = parent.parentElement, depth++) {
                if (count(parent)) applicationCandidates.push(parent);
              }
            }
            const modalCandidates = candidates.filter(node =>
              node.matches('[role="dialog"], [aria-modal="true"], [data-test*="ApplicationModal"], '
                + '[data-testid*="ApplicationModal"], [class*="ApplicationModal"]')
              && count(node) > 0
            );
            const formCandidates = candidates.filter(node => node.matches('form') && count(node) > 0);
            const roots = activeRoot ? [activeRoot]
              : applicationCandidates.length ? applicationCandidates
              : modalCandidates.length ? modalCandidates
              : formCandidates.length ? formCandidates : [document.body];
            roots.sort((a, b) => count(b) - count(a));
            const root = roots[0];
            const scrollables = [root, ...root.querySelectorAll('*')].filter(node => {
              const style = window.getComputedStyle(node);
              return node.scrollHeight > node.clientHeight + 40
                && /(auto|scroll|overlay)/.test(style.overflowY);
            });
            scrollables.sort((a, b) =>
              (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight)
            );
            return scrollables[0] || null;
            """
        )
        if scroller is None:
            return
        original_top = self.driver.execute_script(
            "return arguments[0].scrollTop;", scroller
        )
        try:
            for _ in range(30):
                position = self.driver.execute_script(
                    """
                    const node = arguments[0];
                    const step = Math.max(160, Math.floor(node.clientHeight * 0.75));
                    const next = Math.min(node.scrollTop + step, node.scrollHeight - node.clientHeight);
                    node.scrollTop = next;
                    node.dispatchEvent(new Event('scroll', {bubbles: true}));
                    return {top: node.scrollTop, max: node.scrollHeight - node.clientHeight};
                    """,
                    scroller,
                )
                self._application_questions()
                if position["top"] >= position["max"]:
                    break
                sleep(0.1)
        finally:
            self.driver.execute_script(
                "arguments[0].scrollTop = arguments[1];",
                scroller,
                original_top,
            )

    def _application_form_diagnostics(self) -> dict[str, object]:
        return self.driver.execute_script(
            """
            const selectors = {
              inputs: 'input:not([type="hidden"]):not([type="submit"]):not([type="button"])'
                + ':not([type="search"]):not(#search):not([name="search"])',
              textareas: 'textarea',
              radios: 'input[type="radio"], [role="radio"]',
              checkboxes: 'input[type="checkbox"], [role="checkbox"]',
              dropdowns: 'select, [role="combobox"]',
            };
            const visible = node => {
              const style = window.getComputedStyle(node);
              return node.getClientRects().length > 0
                && style.visibility !== 'hidden' && style.display !== 'none';
            };
            const modalSelector = '[role="dialog"], [aria-modal="true"], '
              + '[data-test*="ApplicationModal"], [data-testid*="ApplicationModal"], '
              + '[class*="ApplicationModal"]';
            const activeRoot = document.querySelector('[data-autowell-active-application="true"]');
            const candidates = activeRoot ? [activeRoot]
              : Array.from(document.querySelectorAll(`${modalSelector}, form`))
              .filter(visible);
            const controlSelector = Object.values(selectors).join(',');
            const applicationCandidates = [];
            for (const button of document.querySelectorAll('button, [role="button"]')) {
              if (!visible(button) || !/send application|submit application/i.test(button.innerText || '')) {
                continue;
              }
              for (let parent = button.parentElement, depth = 0;
                   parent && parent !== document.body && depth < 8;
                   parent = parent.parentElement, depth++) {
                if (parent.querySelectorAll(controlSelector).length) {
                  applicationCandidates.push(parent);
                }
              }
            }
            const modalCandidates = candidates.filter(node =>
              node.matches(modalSelector) && node.querySelectorAll(
                controlSelector
              ).length
            );
            const formCandidates = candidates.filter(node =>
              node.matches('form') && node.querySelectorAll(
                controlSelector
              ).length
            );
            const roots = activeRoot ? [activeRoot]
              : applicationCandidates.length ? applicationCandidates
              : modalCandidates.length ? modalCandidates
              : formCandidates.length ? formCandidates : [];
            if (!roots.length) {
              return {
                modal_found: false, form_found: false, root_tag: '', root_role: '',
                raw_controls: 0, visible_controls: 0,
                counts: {inputs: 0, textareas: 0, radios: 0, checkboxes: 0, dropdowns: 0},
              };
            }
            roots.sort((a, b) =>
              b.querySelectorAll(controlSelector).length
              - a.querySelectorAll(controlSelector).length
            );
            const root = roots[0];
            const modal = modalCandidates.length ? modalCandidates[0] : null;
            const counts = {};
            let raw = 0;
            let shown = 0;
            for (const [name, selector] of Object.entries(selectors)) {
              const nodes = Array.from(root.querySelectorAll(selector));
              counts[name] = name === 'radios'
                ? new Set(nodes.map(node => {
                    const container = node.closest('fieldset, [role="radiogroup"]');
                    return node.name || (container ? container : node.id || node);
                  })).size
                : nodes.length;
              raw += nodes.length;
              shown += nodes.filter(node =>
                visible(node) || Array.from(node.labels || []).some(visible)
                  || Boolean(node.closest('label') && visible(node.closest('label')))
              ).length;
            }
            return {
              modal_found: Boolean(modal),
              form_found: candidates.some(node => node.matches('form')),
              root_tag: root.tagName,
              root_role: root.getAttribute('role') || '',
              raw_controls: raw,
              visible_controls: shown,
              counts,
            };
            """
        )

    def _save_application_form_debug_artifacts(self) -> None:
        debug_dir = Path(__file__).resolve().parents[2] / "form_debug"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            debug_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = debug_dir / f"application-form-{stamp}.png"
            if not self.driver.save_screenshot(str(screenshot_path)):
                self._report("[FORM] Could not save application-form screenshot.")
            html = self.driver.execute_script(
                """
                const selector = 'input:not([type="search"]):not(#search):not([name="search"]), '
                  + 'textarea, select, [role="combobox"], [role="textbox"], '
                  + '[role="radio"], [role="checkbox"], [contenteditable="true"]';
                const modalSelector = '[role="dialog"], [aria-modal="true"], '
                  + '[data-test*="ApplicationModal"], [data-testid*="ApplicationModal"], '
                  + '[class*="ApplicationModal"]';
                const candidates = Array.from(document.querySelectorAll(`${modalSelector}, form`));
                const applicationCandidates = [];
                for (const button of document.querySelectorAll('button, [role="button"]')) {
                  if (!/send application|submit application/i.test(button.innerText || '')) continue;
                  for (let parent = button.parentElement, depth = 0;
                       parent && parent !== document.body && depth < 8;
                       parent = parent.parentElement, depth++) {
                    if (parent.querySelectorAll(selector).length) applicationCandidates.push(parent);
                  }
                }
                const modalCandidates = candidates.filter(node =>
                  node.matches(modalSelector) && node.querySelectorAll(selector).length
                );
                const formCandidates = candidates.filter(node =>
                  node.matches('form') && node.querySelectorAll(selector).length
                );
                const roots = applicationCandidates.length ? applicationCandidates
                  : modalCandidates.length ? modalCandidates
                  : formCandidates.length ? formCandidates : [];
                roots.sort((a, b) =>
                  b.querySelectorAll(selector).length - a.querySelectorAll(selector).length
                );
                const root = roots[0];
                return root ? root.outerHTML : document.body.innerHTML;
                """
            )
            (debug_dir / f"application-form-{stamp}.html").write_text(
                str(html or ""), encoding="utf-8"
            )
            self._report(f"[FORM] Saved local form diagnostics in {debug_dir}.")
        except Exception as error:
            self._report(
                f"[FORM] Could not save form diagnostics ({type(error).__name__})."
            )

    def _application_questions(self) -> list[dict[str, object]]:
        return self.driver.execute_script(
            """
            const controlSelector = 'input:not([type="search"]):not(#search):not([name="search"]), '
              + 'textarea, select, [role="combobox"], '
              + '[role="textbox"], [role="radio"], [role="checkbox"], [contenteditable="true"]';
            const visible = node => {
              const style = window.getComputedStyle(node);
              return node.getClientRects().length > 0
                && style.visibility !== 'hidden' && style.display !== 'none';
            };
            const modalSelector = '[role="dialog"], [aria-modal="true"], '
              + '[data-test*="ApplicationModal"], [data-testid*="ApplicationModal"], '
              + '[class*="ApplicationModal"]';
            const candidates = Array.from(document.querySelectorAll(
              `${modalSelector}, form`
            )).filter(visible);
            const count = node => node.querySelectorAll(controlSelector).length;
            const submitButtons = Array.from(document.querySelectorAll('button, [role="button"]'))
              .filter(button => visible(button)
                && /send application|submit application/i.test(button.innerText || ''));
            const modalCandidates = candidates.filter(node =>
              node.matches(modalSelector) && count(node)
            );
            const formCandidates = candidates.filter(node =>
              node.matches('form') && count(node)
            );
            const activeModals = modalCandidates.filter(node =>
              submitButtons.some(button => node.contains(button))
            );
            const applicationCandidates = [];
            for (const button of submitButtons) {
              for (let parent = button.parentElement;
                   parent && parent !== document.body; parent = parent.parentElement) {
                if (count(parent)) {
                  applicationCandidates.push(parent);
                  break;
                }
              }
            }
            const roots = activeModals.length ? activeModals
              : applicationCandidates.length ? applicationCandidates
              : modalCandidates.length ? modalCandidates : formCandidates;
            roots.sort((a, b) =>
              count(a) - count(b)
              || (a.contains(b) ? 1 : b.contains(a) ? -1 : 0)
            );
            if (!roots.length) {
              document.querySelectorAll('[data-autowell-active-application="true"]')
                .forEach(node => node.removeAttribute('data-autowell-active-application'));
              return [];
            }
            const root = roots[0];
            document.querySelectorAll('[data-autowell-active-application="true"]')
              .forEach(node => node.removeAttribute('data-autowell-active-application'));
            root.setAttribute('data-autowell-active-application', 'true');
            const controls = Array.from(root.querySelectorAll(controlSelector)).filter(node => {
              if (node.disabled || node.getAttribute('aria-disabled') === 'true'
                  || node.getAttribute('aria-hidden') === 'true'
                  || node.type === 'search' || node.id === 'search' || node.name === 'search'
                  || ['hidden', 'submit', 'button', 'reset', 'image'].includes(node.type)) {
                return false;
              }
              return visible(node);
            });
            const groups = [];
            const radios = new Map();
            const radioContainerIds = new WeakMap();
            let nextRadioGroupId = 0;
            const radioKey = node => {
              if (node.name) return `name-${node.name}`;
              const container = node.closest('[role="radiogroup"], fieldset');
              if (container) {
                if (!radioContainerIds.has(container)) {
                  radioContainerIds.set(container, nextRadioGroupId++);
                }
                return `container-${radioContainerIds.get(container)}`;
              }
              return node.id || `radio-${controls.indexOf(node)}`;
            };
            for (const node of controls) {
              if (node.type === 'radio' || node.getAttribute('role') === 'radio') {
                const key = radioKey(node);
                if (!radios.has(key)) {
                  const group = controls.filter(item => item.type === 'radio'
                    || item.getAttribute('role') === 'radio')
                    .filter(item => radioKey(item) === key);
                  radios.set(key, group);
                  groups.push({nodes: group, type: 'radio'});
                }
              } else if (node.type === 'checkbox' || node.getAttribute('role') === 'checkbox') {
                groups.push({nodes: [node], type: 'checkbox'});
              } else {
                groups.push({
                  nodes: [node],
                  type: node.getAttribute('role') === 'combobox'
                    ? 'combobox'
                    : node.isContentEditable
                      ? 'contenteditable'
                      : node.tagName.toLowerCase() === 'select'
                    ? 'select'
                    : node.tagName.toLowerCase() === 'textarea'
                      ? 'textarea'
                      : node.type || 'text',
                });
              }
            }
            return groups.map((group, index) => {
              const first = group.nodes[0];
              const id = String(index);
              for (const node of group.nodes) node.setAttribute('data-autowell-question-id', id);
              const labels = node => [
                ...Array.from(node.labels || []).map(label => label.innerText || label.textContent || ''),
                node.closest('label')?.innerText || '',
                node.getAttribute('aria-label') || '',
                ...(node.getAttribute('aria-labelledby') || '').split(/\\s+/)
                  .map(labelId => document.getElementById(labelId)?.innerText || ''),
              ].filter(Boolean).map(value => value.trim());
              let prompt = [
                ...labels(first),
                first.getAttribute('placeholder') || '',
                first.getAttribute('data-testid') || '',
              ].filter(Boolean).join(' ');
              const questionContainer = first.closest('fieldset, [role="radiogroup"]');
              const legend = questionContainer?.querySelector('legend');
              if (legend) {
                prompt = `${legend.innerText || legend.textContent} ${prompt}`;
              } else if (questionContainer?.getAttribute('aria-label')) {
                prompt = `${questionContainer.getAttribute('aria-label')} ${prompt}`;
              }
              let context = '';
              for (let parent = questionContainer || first.parentElement, depth = 0;
                   parent && parent !== root && depth < 6;
                   parent = parent.parentElement, depth++) {
                const text = (parent.innerText || '').replace(/\\s+/g, ' ').trim();
                const optionText = group.type === 'radio' || group.type === 'checkbox'
                  ? group.nodes.map(node =>
                      (node.closest('label')?.innerText || node.value || '').trim()
                    ).filter(Boolean)
                  : [];
                const questionText = optionText.reduce((value, option) =>
                  value.replace(option, ''), text).replace(/\\s+/g, ' ').trim();
                if (questionText.length > 3 && questionText.length < 900) {
                  context = questionText;
                  if (questionText.length <= 550) prompt = `${prompt} ${questionText}`;
                  break;
                }
              }
              const nativeOptions = group.type === 'radio' || group.type === 'checkbox'
                ? group.nodes.map(node => ({
                    value: node.value,
                    label: labels(node).join(' ').trim() || node.value,
                  }))
                : group.type === 'select'
                  ? Array.from(first.options).filter(option => option.value).map(option => ({
                      value: option.value,
                      label: option.text.trim(),
                    }))
                  : [];
              const controlledListId = first.getAttribute('aria-controls');
              const controlledList = controlledListId && document.getElementById(controlledListId);
              const customOptions = controlledList
                ? Array.from(controlledList.querySelectorAll('[role="option"], li'))
                    .map(option => ({
                      value: option.getAttribute('data-value') || option.getAttribute('value') || option.innerText.trim(),
                      label: option.innerText.trim(),
                    }))
                : [];
              const fullPrompt = `${prompt} ${context}`.replace(/\\s+/g, ' ').trim();
              const explicitCoverLetter = /cover\\s*letter/i.test(fullPrompt);
              return {
                id,
                prompt: (fullPrompt || first.placeholder || first.name || `Application question ${index + 1}`).slice(0, 900),
                label: labels(first).join(' ').trim(),
                placeholder: first.getAttribute('placeholder') || '',
                aria_label: first.getAttribute('aria-label') || '',
                context: context.slice(0, 700),
                type: group.type,
                required: group.nodes.some(node => node.required || node.getAttribute('aria-required') === 'true'),
                options: nativeOptions.length ? nativeOptions : customOptions,
                cover_letter: explicitCoverLetter,
                checked: group.nodes.some(node =>
                  node.checked || node.getAttribute('aria-checked') === 'true'
                ),
                value: group.type === 'radio'
                  ? (group.nodes.find(node => node.checked || node.getAttribute('aria-checked') === 'true')?.value || '')
                  : group.type === 'checkbox'
                    ? (first.checked || first.getAttribute('aria-checked') === 'true' ? 'checked' : '')
                    : group.type === 'select'
                      ? (first.selectedOptions[0]?.value || '')
                      : first.value || first.innerText || '',
              };
            });
            """
        )

    @staticmethod
    def _resume_contact_answer(question: str, resume_text: str) -> str:
        normalized_question = question.casefold()
        if re.search(r"\b(email|e-mail)\b", normalized_question):
            match = re.search(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", resume_text, re.IGNORECASE)
            return match.group(0) if match else ""
        if re.search(r"\b(phone|mobile|telephone|contact number)\b", normalized_question):
            for match in re.finditer(r"\+?[\d][\d\s().-]{6,}\d", resume_text):
                digits = re.sub(r"\D", "", match.group(0))
                if 8 <= len(digits) <= 15:
                    return match.group(0).strip()
        return ""

    def _profile_answer(self, question: str) -> str:
        key, answer = match_profile_answer(question, self.profile_answers)
        if key == "referral_details" and not answer:
            if self.profile_answers.get("referred", "").strip().casefold() == "no":
                return "N/A"
        return answer

    @staticmethod
    def _question_text(question: dict[str, object]) -> str:
        return str(
            question.get("prompt")
            or question.get("label")
            or question.get("aria_label")
            or question.get("placeholder")
            or "Unlabeled application question"
        ).strip()

    @staticmethod
    def _option_index(answer: str, options: list[dict[str, str]]) -> int | None:
        normalized_answer = normalize_question(answer)
        yes_values = {"yes", "y", "true", "i agree", "agree", "authorized", "authorised"}
        no_values = {"no", "n", "false", "not applicable", "n a"}
        for index, option in enumerate(options):
            label = str(option.get("label", ""))
            value = str(option.get("value", ""))
            normalized_label = normalize_question(label)
            normalized_value = normalize_question(value)
            if normalized_answer in {normalized_label, normalized_value}:
                return index
            if normalized_answer in yes_values and normalized_label in yes_values | {"authorized to work", "authorised to work"}:
                return index
            if normalized_answer in no_values and normalized_label in no_values:
                return index
        contained = [
            index for index, option in enumerate(options)
            if normalized_answer
            and (
                normalized_answer in normalize_question(str(option.get("label", "")))
                or normalize_question(str(option.get("label", ""))) in normalized_answer
            )
        ]
        return contained[0] if len(contained) == 1 else None

    def _ai_question_answers(
        self,
        description: str,
        questions: list[dict[str, object]],
    ) -> dict[str, dict[str, str]]:
        if getattr(self, "dry_run", False):
            return {}
        eligible = [
            question for question in questions
            if question["type"] in {"text", "textarea", "email", "tel", "url", "contenteditable"}
            and not question.get("cover_letter")
            and not any(
                phrase in normalize_question(self._question_text(question))
                for phrase in ("additional information", "anything else", "cover letter")
            )
            and not is_sensitive_question(self._question_text(question))
            and not self._profile_answer(self._question_text(question))
        ]
        if not eligible:
            return {}
        try:
            answers = self.cover_letter_generator.answer_application_questions(
                description,
                eligible,
                profile_answers=self.profile_answers,
            )
        except Exception as error:
            self._report(f"[FORM] AI question suggestions unavailable ({type(error).__name__}).")
            return {}
        return {str(answer["id"]): answer for answer in answers}

    def _fill_form_field(
        self,
        question: dict[str, object],
        answer: str,
    ) -> bool:
        question_id = str(question["id"])
        elements = self._application_field_elements(question_id)
        if not elements:
            self._report(
                f"[FORM] No field with id={question_id!r} exists inside the active application form."
            )
            return False
        field = elements[0]
        if not self._field_is_interactable(field, question_id):
            return False
        elements = self._application_field_elements(question_id)
        if not elements:
            self._report(f"[FORM] Field id={question_id!r} detached after scrolling; skipped.")
            return False
        field = elements[0]
        element_details = (
            f"id={field.get_attribute('id')!r} "
            f"name={field.get_attribute('name')!r} "
            f"type={field.get_attribute('type')!r}"
        )
        question_type = str(question["type"])
        options = question.get("options", [])

        if question_type in {"radio", "select", "combobox"}:
            option_elements = []
            if question_type == "combobox" and not options:
                field.click()
                visible_options = []
                option_elements = []
                for candidate in self.driver.find_elements(By.CSS_SELECTOR, '[role="option"]'):
                    if candidate.is_displayed() and candidate.is_enabled():
                        option_elements.append(candidate)
                        visible_options.append({
                            "label": candidate.text.strip(),
                            "value": candidate.get_attribute("data-value") or candidate.text.strip(),
                        })
                options = visible_options
            matched_index = self._option_index(answer, options)
            if matched_index is None:
                return False
            option = options[matched_index]
            if question_type == "radio":
                if matched_index >= len(elements):
                    return False
                target = elements[matched_index]
                selected = bool(self.driver.execute_script(
                    "return arguments[0].checked || arguments[0].getAttribute('aria-checked') === 'true';",
                    target,
                ))
                if not selected:
                    self.driver.execute_script(
                        """
                        const input = arguments[0];
                        const label = Array.from(input.labels || [])
                          .find(node => node.getClientRects().length > 0);
                        if (label) label.click();
                        if (!input.checked) input.click();
                        """,
                        target,
                    )
                current = self._application_field_elements(question_id)
                return len(current) > matched_index and bool(self.driver.execute_script(
                    "return arguments[0].checked || arguments[0].getAttribute('aria-checked') === 'true';",
                    current[matched_index],
                ))
            if question_type == "select":
                select = Select(field)
                try:
                    select.select_by_visible_text(str(option.get("label", "")))
                except Exception:
                    select.select_by_value(str(option.get("value", "")))
                selected = self.driver.execute_script(
                    """
                    const selected = arguments[0].selectedOptions[0];
                    return selected ? {value: selected.value, label: selected.text.trim()} : null;
                    """,
                    field,
                )
                return bool(selected) and (
                    normalize_question(str(selected.get("value", "")))
                    == normalize_question(str(option.get("value", "")))
                    or normalize_question(str(selected.get("label", "")))
                    == normalize_question(str(option.get("label", "")))
                )
            if not option_elements:
                field.click()
                option_elements = [
                    candidate for candidate in self.driver.find_elements(By.CSS_SELECTOR, '[role="option"]')
                    if candidate.is_displayed() and candidate.is_enabled()
                ]
            for candidate in option_elements:
                if normalize_question(candidate.text) == normalize_question(str(option.get("label", ""))):
                    candidate.click()
                    selected_text = self.driver.execute_script(
                        """
                        return arguments[0].value || arguments[0].innerText
                          || arguments[0].getAttribute('aria-valuetext') || '';
                        """,
                        field,
                    )
                    selected = self.driver.execute_script(
                        "return arguments[0].getAttribute('aria-selected') === 'true';",
                        candidate,
                    )
                    return bool(selected) or (
                        normalize_question(str(selected_text))
                        == normalize_question(str(option.get("label", "")))
                    )
            return False

        if question_type == "checkbox":
            normalized = normalize_question(answer)
            if normalized in {"yes", "true", "agree", "i agree", "accept", "checked"}:
                should_check = True
            elif normalized in {"no", "false", "disagree", "decline", "unchecked"}:
                should_check = False
            else:
                label = str((options or [{}])[0].get("label", ""))
                if normalized != normalize_question(label):
                    return False
                should_check = True
            checked = bool(self.driver.execute_script(
                "return arguments[0].checked || arguments[0].getAttribute('aria-checked') === 'true';",
                field,
            ))
            if checked != should_check:
                field.click()
            return bool(self.driver.execute_script(
                "return arguments[0].checked || arguments[0].getAttribute('aria-checked') === 'true';",
                field,
            )) == should_check

        if question_type not in {"text", "textarea", "email", "tel", "number", "url", "contenteditable"}:
            return False
        self._report(f"[FORM] Filling {element_details}.")
        field.clear()
        field.send_keys(answer)
        value = self.driver.execute_script(
            "return arguments[0].value || arguments[0].innerText || '';",
            field,
        )
        return self._normalized_answer(str(value)) == self._normalized_answer(answer)

    def _answer_application_questions(self, description: str) -> list[str]:
        self._report("[FORM] Application form detected.")
        initial = self._wait_for_application_fields()
        total_fields = len(initial)
        self._report(f"[FORM] {total_fields} fields identified.")
        all_unanswered: dict[str, dict[str, object]] = {}
        proposed_answers: list[dict[str, str]] = []
        answered_signatures: set[str] = set()
        answered_count = 0
        interaction_failed = False
        if getattr(self, "cover_letter_inserted", False):
            answered_count = 1
        cover_letter_detected = any(question.get("cover_letter") for question in initial)
        current_fields = initial

        for _scan in range(5):
            total_fields = max(total_fields, len(current_fields))
            cover_letter_detected = cover_letter_detected or any(
                question.get("cover_letter") for question in current_fields
            )
            ai_answers = self._ai_question_answers(description, current_fields)
            for question in current_fields:
                if question.get("cover_letter"):
                    answered_signatures.add(
                        normalize_question(self._question_text(question))
                        + "|cover_letter"
                    )
                    continue
                prompt = self._question_text(question)
                signature = normalize_question(prompt) + "|" + str(question["type"])
                if signature in answered_signatures:
                    continue
                answer = self._profile_answer(prompt)
                source = "profile"
                if not answer:
                    _saved_question, answer = match_saved_question(prompt, self.question_answers)
                    source = "saved"
                if not answer and re.search(
                    r"\b(email|e-mail|phone|mobile|telephone|contact number)\b",
                    prompt,
                    re.IGNORECASE,
                ):
                    resume_text = self.cover_letter_generator.get_resume_text()
                    answer = self._resume_contact_answer(prompt, resume_text)
                    source = "resume"
                if not answer and not is_sensitive_question(prompt):
                    generated = ai_answers.get(str(question["id"]), {})
                    answer = str(generated.get("answer", "")).strip()
                    source = "ai"
                if not answer and str(question["type"]) == "checkbox":
                    answer = ""

                if answer and self.dry_run:
                    answered_count += 1
                    answered_signatures.add(signature)
                    question["proposed_answer"] = answer
                    proposed_answers.append({
                        "question": prompt,
                        "answer": answer,
                        "type": str(question["type"]),
                    })
                    self._report(
                        f"[DRY RUN] Proposed for {prompt[:110]}: {answer[:180]}"
                    )
                    continue

                if answer:
                    try:
                        if self._fill_form_field(question, answer):
                            answered_count += 1
                            answered_signatures.add(signature)
                            self._report(f"[FORM] {prompt[:90]} matched ({source}).")
                            continue
                    except WebDriverException as error:
                        interaction_failed = True
                        field_id = str(question.get("id", ""))
                        fields = self._application_field_elements(field_id)
                        self._log_field_interaction_failure(
                            fields[0] if fields else None, error
                        )

                if question["type"] in {"radio", "checkbox"}:
                    already_filled = bool(question.get("checked"))
                else:
                    already_filled = bool(str(question.get("value", "")).strip())
                if already_filled:
                    answered_signatures.add(signature)
                    continue

                all_unanswered.setdefault(signature, {
                    "question": prompt,
                    "type": str(question["type"]),
                    "options": ", ".join(
                        str(option.get("label", ""))
                        for option in question.get("options", [])
                    ),
                    "required": "yes" if question.get("required") else "no",
                })
                self._report(
                    f"[FORM] Unmapped question ({question['type']}, "
                    f"{'required' if question.get('required') else 'optional'}): "
                    f"{prompt[:160]}"
                )

            try:
                updated_fields = self._application_questions()
            except StaleElementReferenceException:
                updated_fields = self._wait_for_application_fields()
            old_signature = [
                (normalize_question(self._question_text(item)), str(item["type"]))
                for item in current_fields
            ]
            new_signature = [
                (normalize_question(self._question_text(item)), str(item["type"]))
                for item in updated_fields
            ]
            if old_signature == new_signature:
                break
            current_fields = updated_fields

        required_unknown = [
            item["question"] for item in all_unanswered.values()
            if item["required"] == "yes"
        ]
        if all_unanswered and self.question_callback:
            self.question_callback(list(all_unanswered.values()))
        summary = {
            "dry_run": self.dry_run,
            "total": total_fields,
            "answered": 0 if self.dry_run else answered_count,
            "proposed": answered_count if self.dry_run else 0,
            "proposed_answers": proposed_answers,
            "skipped": max(0, total_fields - answered_count - len(all_unanswered)),
            "unknown": len(all_unanswered),
            "unknown_questions": list(all_unanswered.values()),
            "cover_letter_detected": cover_letter_detected,
            "cover_letter_inserted": getattr(self, "cover_letter_inserted", False),
            "validation": "needs review" if required_unknown or interaction_failed else "ready",
        }
        self.form_summary = summary
        if self.form_callback:
            self.form_callback(summary)
        if self.dry_run:
            self._report(f"[DRY RUN] {answered_count}/{total_fields} proposed answers; form unchanged.")
        else:
            self._report(f"[FORM] {answered_count}/{total_fields} fields completed.")
        if all_unanswered:
            self._report(f"[FORM] {len(all_unanswered)} unknown question(s) require review.")
        if required_unknown or interaction_failed:
            self.pause_for_review = True
        return required_unknown

    @staticmethod
    def _normalized_answer(answer: str) -> str:
        return " ".join(answer.casefold().split())

    def _advance_application_step(self) -> bool:
        next_labels = re.compile(
            r"^(?:next|continue|continue application|save and continue|review application)$",
            re.IGNORECASE,
        )
        buttons = self.driver.execute_script(
            """
            const root = document.querySelector('[data-autowell-active-application="true"]');
            return root ? Array.from(root.querySelectorAll('button, [role="button"]')) : [];
            """
        ) or []
        for button in buttons:
            if not button.is_displayed() or not button.is_enabled():
                continue
            label = " ".join((button.text or "").casefold().split())
            if not next_labels.fullmatch(label):
                continue
            before = [
                (normalize_question(self._question_text(question)), str(question["type"]))
                for question in self._application_questions()
            ]
            button.click()

            def step_changed(_driver):
                try:
                    current = [
                        (normalize_question(self._question_text(question)), str(question["type"]))
                        for question in self._application_questions()
                    ]
                    return current != before or not button.is_displayed()
                except StaleElementReferenceException:
                    return True

            try:
                WebDriverWait(self.driver, 8, poll_frequency=0.25).until(step_changed)
            except TimeoutException:
                self._report("[FORM] Continue button did not advance to another form step.")
                return False
            self._report("[FORM] New application step detected; rescanning fields.")
            return True
        return False

    def _validate_application_form(self) -> list[str]:
        invalid_fields = self.driver.execute_script(
            """
            const visible = node => {
              const style = window.getComputedStyle(node);
              return node.getClientRects().length > 0
                && style.visibility !== 'hidden' && style.display !== 'none';
            };
            const selector = 'input:not([type="search"]):not(#search):not([name="search"]), '
              + 'textarea, select, [role="combobox"], [role="textbox"], '
              + '[role="radio"], [role="checkbox"], [contenteditable="true"]';
            const modalSelector = '[role="dialog"], [aria-modal="true"], '
              + '[data-test*="ApplicationModal"], [data-testid*="ApplicationModal"], '
              + '[class*="ApplicationModal"]';
            const activeRoot = document.querySelector('[data-autowell-active-application="true"]');
            const candidates = activeRoot ? [activeRoot]
              : Array.from(document.querySelectorAll(`${modalSelector}, form`))
              .filter(visible);
            const applicationCandidates = [];
            for (const button of document.querySelectorAll('button, [role="button"]')) {
              if (!visible(button) || !/send application|submit application/i.test(button.innerText || '')) {
                continue;
              }
              for (let parent = button.parentElement, depth = 0;
                   parent && parent !== document.body && depth < 8;
                   parent = parent.parentElement, depth++) {
                if (parent.querySelectorAll(selector).length) applicationCandidates.push(parent);
              }
            }
            const modalCandidates = candidates.filter(node =>
              node.matches(modalSelector) && node.querySelectorAll(selector).length
            );
            const formCandidates = candidates.filter(node =>
              node.matches('form') && node.querySelectorAll(selector).length
            );
            const roots = activeRoot ? [activeRoot]
              : applicationCandidates.length ? applicationCandidates
              : modalCandidates.length ? modalCandidates
              : formCandidates.length ? formCandidates : [];
            if (!roots.length) return ['Application form'];
            const rootScore = node => node.querySelectorAll(selector).length * 10
              + (node.matches(modalSelector) ? 100 : 0)
              + (/your application|send application/i.test(node.innerText || '') ? 30 : 0);
            roots.sort((a, b) => rootScore(b) - rootScore(a));
            const root = roots[0];
            const controls = Array.from(root.querySelectorAll(selector))
              .filter(node => visible(node) && !node.disabled);
            const invalid = new Set();
            const containerIds = new WeakMap();
            let nextContainerId = 0;
            const radioKey = node => {
              if (node.name) return `name-${node.name}`;
              const container = node.closest('fieldset, [role="radiogroup"]');
              if (container) {
                if (!containerIds.has(container)) containerIds.set(container, nextContainerId++);
                return `container-${containerIds.get(container)}`;
              }
              return node.id || `single-${controls.indexOf(node)}`;
            };
            const labelFor = node => Array.from(node.labels || [])
              .map(item => item.innerText || item.textContent || '').join(' ')
              || node.getAttribute('aria-label') || node.placeholder || node.name || node.type;
            for (const node of controls) {
              if (node.type === 'search' || node.id === 'search' || node.name === 'search') continue;
              const required = node.required || node.getAttribute('aria-required') === 'true';
              if (!required) continue;
              if (node.type === 'radio' || node.getAttribute('role') === 'radio') {
                const group = controls.filter(item =>
                  (item.type === 'radio' || item.getAttribute('role') === 'radio')
                  && radioKey(item) === radioKey(node)
                );
                if (!group.some(item => item.checked || item.getAttribute('aria-checked') === 'true')) {
                  invalid.add(labelFor(node));
                }
              } else if (node.type === 'checkbox' || node.getAttribute('role') === 'checkbox') {
                if (!(node.checked || node.getAttribute('aria-checked') === 'true')) {
                  invalid.add(labelFor(node));
                }
              } else if (typeof node.checkValidity === 'function' && !node.checkValidity()) {
                invalid.add(labelFor(node));
              } else if (
                node.getAttribute('aria-required') === 'true'
                && !(node.value || node.innerText || '').trim()
              ) {
                invalid.add(labelFor(node));
              }
            }
            return Array.from(invalid);
            """
        )
        invalid_fields = list(invalid_fields or [])
        for question in self._application_questions():
            if not question.get("required"):
                continue
            value = str(question.get("value", "")).strip()
            if question["type"] in {"radio", "checkbox"}:
                complete = bool(question.get("checked"))
            else:
                complete = bool(value)
            if not complete:
                label = self._question_text(question)
                if label not in invalid_fields:
                    invalid_fields.append(label)
        if invalid_fields:
            self._report(f"[FORM] Validation blocked submission for {len(invalid_fields)} required field(s).")
        else:
            self._report("[FORM] Required-field validation passed.")
        return [str(field) for field in invalid_fields]

    def _submit_application(self) -> None:
        def find_submit_button(driver):
            root = driver.execute_script(
                "return document.querySelector('[data-autowell-active-application=\"true\"]');"
            )
            if root is None:
                return False
            for candidate in root.find_elements(By.CSS_SELECTOR, "button, [role='button']"):
                if not candidate.is_displayed() or not candidate.is_enabled():
                    continue
                label = " ".join(candidate.text.casefold().split())
                if "send application" in label or "submit application" in label or (
                    candidate.get_attribute("type") == "submit"
                ):
                    return candidate
            return False

        try:
            button = WebDriverWait(self.driver, 15).until(find_submit_button)
        except Exception as error:
            raise RuntimeError(
                "The application form did not expose an enabled Submit button."
            ) from error

        button.click()

        def submission_result(driver):
            body = driver.find_element(By.TAG_NAME, "body").text.casefold()
            if re.search(
                r"application (?:was )?(?:submitted|sent)|successfully applied|application received",
                body,
            ):
                return "submitted"
            if re.search(r"already applied|application already exists", body):
                return "already_applied"
            if re.search(r"please complete|required field|something went wrong|unable to submit", body):
                return "rejected"
            return False

        try:
            result = WebDriverWait(self.driver, 15).until(submission_result)
        except Exception as error:
            raise RuntimeError(
                "Wellfound did not confirm whether the application was submitted."
            ) from error
        if result == "already_applied":
            raise RuntimeError("Wellfound reports that this application was already submitted.")
        if result == "rejected":
            raise RuntimeError(
                "Wellfound rejected the application or reported required fields; "
                "submission was not confirmed."
            )

    def process_link(
        self,
        link: str,
        job: dict[str, str] | None = None,
    ) -> tuple[bool, str | None]:
        self.dry_run = getattr(self, "dry_run", False)
        description = ""
        cover_letter = ""
        provider_used = ""
        status = "Failed"
        error_message = ""
        self.cover_letter_inserted = False
        try:
            self.driver.get(link)
            description = self._job_description()
            if self.dry_run:
                self._report(
                    "[DRY RUN] Cover-letter generation and external AI requests are disabled."
                )
            else:
                generation = self.cover_letter_generator.generate_cover_letter_result(
                    {
                        "role": (job or {}).get("role", ""),
                        "company": (job or {}).get("company", ""),
                        "description": description,
                    }
                )
                cover_letter = generation["cover_letter"]
                provider_used = generation["provider_used"]
                self.generation_provider_counts[provider_used] = (
                    self.generation_provider_counts.get(provider_used, 0) + 1
                )
                if provider_used == "template":
                    self._report(
                        "AI providers were unavailable; using the built-in cover-letter template."
                    )
                else:
                    self._report(f"Cover letter generated with {provider_used.title()}.")
            self._click_apply()
            self._wait_for_application_fields()
            if self.dry_run:
                cover_letter_field_found = self._cover_letter_field() is not None
                self._report(
                    "[DRY RUN] "
                    + (
                        "Cover-letter field detected; generated letter was not inserted."
                        if cover_letter_field_found
                        else "No explicit cover-letter field detected; no letter inserted."
                    )
                )
            else:
                self.cover_letter_inserted = self._insert_cover_letter_if_present(cover_letter)
            for step in range(5):
                unanswered = self._answer_application_questions(description)
                if self.dry_run:
                    status = "Dry run (not submitted)"
                    if unanswered:
                        error_message = (
                            "Manual review required for: " + "; ".join(unanswered[:5])
                        )
                    break
                if unanswered:
                    status = "Needs review"
                    error_message = (
                        "Required application questions need a confirmed answer: "
                        + "; ".join(unanswered[:5])
                    )
                    self._report("[FORM] Submission paused until required answers are reviewed.")
                    break
                if getattr(self, "pause_for_review", False):
                    status = "Needs review"
                    error_message = (
                        "A form field interaction failed; review the open application before continuing."
                    )
                    self._report("[FORM] Submission paused after a field interaction failure.")
                    break
                if self._advance_application_step():
                    self._wait_for_application_fields()
                    continue
                invalid_fields = self._validate_application_form()
                if invalid_fields:
                    status = "Needs review"
                    error_message = (
                        "Required form validation failed: "
                        + "; ".join(invalid_fields[:5])
                    )
                    self.pause_for_review = True
                    break
                if self.auto_submit:
                    self._submit_application()
                    status = "Submitted"
                else:
                    status = "Drafted (not submitted)"
                break
            else:
                status = "Needs review"
                error_message = "Application form has too many steps; review it manually."
                self.pause_for_review = True
        except Exception as error:
            error_message = str(error).strip() or type(error).__name__
            if isinstance(error, WebDriverException):
                self._report(
                    f"[FORM] Selenium workflow error ({type(error).__name__}); "
                    "saving application-form diagnostics."
                )
                self._save_application_form_debug_artifacts()

        try:
            self.excel_handler.add_application(
                link,
                description,
                cover_letter,
                status,
                error_message,
                provider_used,
            )
            self.excel_handler.save_excel()
        except Exception as error:
            logging_error = f"Could not save application record: {error}"
            error_message = f"{error_message}; {logging_error}".strip("; ")
            if status != "Submitted":
                status = "Failed"

        self._report(f"{status}: {link}" + (f" — {error_message}" if error_message else ""))
        print(f"{status}: {link}" + (f": {error_message}" if error_message else ""))
        return status == "Submitted", error_message or None

    def process_selected_jobs(
        self,
        jobs: list[dict[str, str]],
    ) -> list[tuple[str, bool, str | None]]:
        results = []
        try:
            total = len(jobs)
            for index, job in enumerate(jobs, start=1):
                link = job["link"]
                self._report(f"Processing application {index} of {total}: {job.get('role', link)}")
                submitted, error = self.process_link(link, job)
                results.append((link, submitted, error))
                if getattr(self, "pause_for_review", False):
                    break
        finally:
            if not getattr(self, "pause_for_review", False):
                self.driver.quit()
        return results

    def process_links(self, data):
        selected_links = [job["link"] for job in data if job.get("checked") == "1"]
        if not selected_links:
            print("No jobs were selected.")
            self.driver.quit()
            return
        if not self.auto_submit:
            print("Draft mode is enabled. Set AUTO_SUBMIT=true in .env to submit applications.")
        try:
            for link in selected_links:
                self.process_link(link)
                if getattr(self, "pause_for_review", False):
                    break
        finally:
            if not getattr(self, "pause_for_review", False):
                self.driver.quit()
