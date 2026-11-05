import os
import re
from collections.abc import Callable

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.firefox import GeckoDriverManager
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions

from autoapply.helper.excel_handler import ExcelHandler
from autoapply.helper.genai import CoverLetterGenerator


load_dotenv()


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
        self.generation_provider_counts: dict[str, int] = {}
        self.auto_submit = (
            os.getenv("AUTO_SUBMIT", "false").strip().lower() == "true"
            if auto_submit is None
            else auto_submit
        )
        self.driver = webdriver.Firefox(
            service=FirefoxService(executable_path=GeckoDriverManager().install()),
            options=self.options,
        )

    def _report(self, message: str) -> None:
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

    def _cover_letter_field(self):
        for selector in (
            "[data-test='JobApplicationModal--CoverLetter']",
            "textarea",
            "[contenteditable='true']",
            "[class*='component'][class*='kpbt6']",
        ):
            for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                if element.is_displayed() and element.is_enabled():
                    return element

    def _wait_for_cover_letter_field(self):
        try:
            return WebDriverWait(self.driver, 15).until(lambda _driver: self._cover_letter_field())
        except Exception as error:
            raise RuntimeError(
                "The application form did not expose a writable cover-letter field."
            ) from error

    def _application_questions(self) -> list[dict[str, object]]:
        return self.driver.execute_script(
            """
            const root = document.querySelector('[role="dialog"]')
              || document.querySelector('[data-test*="JobApplicationModal"]')
              || document.body;
            const visible = node => {
              const style = window.getComputedStyle(node);
              return node.getClientRects().length > 0
                && style.visibility !== 'hidden' && style.display !== 'none';
            };
            const controls = Array.from(root.querySelectorAll('input, textarea, select'))
              .filter(node => visible(node) && !['hidden', 'submit', 'button', 'reset'].includes(node.type));
            const groups = [];
            const radios = new Map();
            for (const node of controls) {
              if (node.type === 'radio') {
                const key = node.name || node.id || `radio-${controls.indexOf(node)}`;
                if (!radios.has(key)) {
                  const group = controls.filter(item => item.type === 'radio'
                    && (item.name || item.id || `radio-${controls.indexOf(item)}`) === key);
                  radios.set(key, group);
                  groups.push({nodes: group, type: 'radio'});
                }
              } else {
                groups.push({
                  nodes: [node],
                  type: node.tagName.toLowerCase() === 'select'
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
                ...Array.from(node.labels || []).map(label => label.innerText),
                node.getAttribute('aria-label') || '',
                ...(node.getAttribute('aria-labelledby') || '').split(/\\s+/)
                  .map(labelId => document.getElementById(labelId)?.innerText || ''),
              ].filter(Boolean);
              let prompt = '';
              const fieldset = first.closest('fieldset');
              if (fieldset?.querySelector('legend')) prompt = fieldset.querySelector('legend').innerText;
              if (!prompt && group.type !== 'radio') prompt = labels(first).join(' ');
              if (!prompt) {
                for (let parent = first.parentElement, depth = 0;
                     parent && parent !== root && depth < 5;
                     parent = parent.parentElement, depth++) {
                  const text = (parent.innerText || '').replace(/\\s+/g, ' ').trim();
                  const optionText = group.nodes.map(node => labels(node).join(' ')).join(' ');
                  if (text.length > optionText.length + 3 && text.length < 700) {
                    prompt = text;
                    break;
                  }
                }
              }
              const options = group.type === 'radio'
                ? group.nodes.map(node => ({
                    value: node.value,
                    label: labels(node).join(' ').trim(),
                  }))
                : group.type === 'select'
                  ? Array.from(first.options).filter(option => option.value).map(option => ({
                      value: option.value,
                      label: option.text.trim(),
                    }))
                  : [];
              return {
                id,
                prompt: (prompt || first.placeholder || first.name || `Application question ${index + 1}`)
                  .replace(/\\s+/g, ' ').trim(),
                type: group.type,
                required: group.nodes.some(node => node.required || node.getAttribute('aria-required') === 'true'),
                options,
              };
            }).filter(question => !(
              question.type === 'textarea' && question.prompt.toLowerCase().includes('cover letter')
            ));
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
        normalized = question.casefold()
        if re.search(r"\b(referrer|person that referred|referral name|referral contact)\b", normalized):
            referral_details = self.profile_answers.get("referral_details", "").strip()
            if referral_details:
                return referral_details
            if self.profile_answers.get("referred", "").strip().casefold() == "no":
                return "N/A"
            return ""
        if re.search(r"\b(refer(?:red|ral)?)\b", normalized):
            return self.profile_answers.get("referred", "").strip()
        if re.search(r"\b(visa|sponsor(?:ship)?|work authorization|legally authorized)\b", normalized):
            return self.profile_answers.get("visa_sponsorship", "").strip()
        if re.search(r"\b(relocat(?:e|ion)|move to)\b", normalized):
            return self.profile_answers.get("relocation", "").strip()
        if re.search(r"\b(phone|mobile|telephone|contact number)\b", normalized):
            return self.profile_answers.get("phone", "").strip()
        return ""

    def _answer_application_questions(self, description: str) -> list[str]:
        questions = self._application_questions()
        if not questions:
            return []
        resume_text = self.cover_letter_generator.get_resume_text()
        answers = self.cover_letter_generator.answer_application_questions(
            description,
            questions,
            profile_answers=self.profile_answers,
        )
        answer_by_id = {item["id"]: item for item in answers}
        unanswered = []

        for question in questions:
            question_id = str(question["id"])
            prompt = str(question["prompt"])
            generated = answer_by_id.get(question_id, {})
            answer = str(generated.get("answer", "")).strip()
            evidence = str(generated.get("evidence", "")).strip()
            configured_answer = self._profile_answer(prompt)
            if configured_answer:
                answer = configured_answer
            elif re.search(r"\b(phone|mobile|telephone|contact number)\b", prompt, re.IGNORECASE):
                answer = self._resume_contact_answer(prompt, resume_text)

            if not configured_answer and re.search(
                r"\b(refer(?:red|ral)?|sponsor(?:ship)?|visa|work authorization|"
                r"relocat(?:e|ion)|salary|compensation|legally authorized)\b",
                prompt,
                re.IGNORECASE,
            ):
                evidence_text = " ".join(evidence.casefold().split())
                resume_normalized = " ".join(resume_text.casefold().split())
                evidence_topics = {
                    "referral": r"\brefer(?:red|ral)?\b",
                    "sponsorship": r"\b(?:sponsor(?:ship)?|visa|work authorization|legally authorized)\b",
                    "relocation": r"\brelocat(?:e|ion)\b",
                    "salary": r"\b(?:salary|compensation)\b",
                }
                topic = next(
                    (
                        topic_name for topic_name, pattern in evidence_topics.items()
                        if re.search(pattern, prompt, re.IGNORECASE)
                    ),
                    None,
                )
                topic_pattern = evidence_topics.get(topic, "")
                if (
                    not evidence_text
                    or evidence_text not in resume_normalized
                    or not re.search(topic_pattern, evidence_text, re.IGNORECASE)
                ):
                    answer = ""

            if not answer:
                if question["required"]:
                    unanswered.append(prompt)
                continue

            selector = f'[data-autowell-question-id="{question_id}"]'
            elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
            if not elements:
                if question["required"]:
                    unanswered.append(prompt)
                continue

            question_type = str(question["type"])
            if question_type == "radio":
                matched_index = next(
                    (
                        index for index, metadata in enumerate(question["options"])
                        if self._normalized_answer(answer) in {
                            self._normalized_answer(str(metadata["label"])),
                            self._normalized_answer(str(metadata["value"])),
                        }
                    ),
                    None,
                )
                if matched_index is None or matched_index >= len(elements):
                    if question["required"]:
                        unanswered.append(prompt)
                    continue
                option = elements[matched_index]
                try:
                    self.driver.execute_script(
                        """
                        const input = arguments[0];
                        if (input.checked) return true;
                        const labels = Array.from(input.labels || []);
                        const label = labels.find(node => node.getClientRects().length > 0);
                        if (label) label.click();
                        if (!input.checked) input.click();
                        return input.checked;
                        """,
                        option,
                    )
                    def target_selected(_driver):
                        try:
                            current_options = self.driver.find_elements(
                                By.CSS_SELECTOR, selector
                            )
                            return (
                                len(current_options) > matched_index
                                and current_options[matched_index].is_selected()
                            )
                        except StaleElementReferenceException:
                            return False

                    WebDriverWait(self.driver, 5).until(target_selected)
                except Exception:
                    if question["required"]:
                        unanswered.append(prompt)
            elif question_type == "select":
                matched_option = next(
                    (
                        metadata for metadata in question["options"]
                        if self._normalized_answer(answer) in {
                            self._normalized_answer(str(metadata["label"])),
                            self._normalized_answer(str(metadata["value"])),
                        }
                    ),
                    None,
                )
                if matched_option is None:
                    if question["required"]:
                        unanswered.append(prompt)
                    continue
                try:
                    select = Select(elements[0])
                    try:
                        select.select_by_visible_text(str(matched_option["label"]))
                    except Exception:
                        select.select_by_value(str(matched_option["value"]))
                except Exception:
                    if question["required"]:
                        unanswered.append(prompt)
                    continue
                try:
                    selected_option = self.driver.execute_script(
                        """
                        const option = arguments[0].selectedOptions[0];
                        return option ? {value: option.value, label: option.text.trim()} : null;
                        """,
                        elements[0],
                    )
                    if not selected_option or not (
                        self._normalized_answer(str(selected_option["value"]))
                        == self._normalized_answer(str(matched_option["value"]))
                        or self._normalized_answer(str(selected_option["label"]))
                        == self._normalized_answer(str(matched_option["label"]))
                    ):
                        if question["required"]:
                            unanswered.append(prompt)
                except Exception:
                    if question["required"]:
                        unanswered.append(prompt)
            elif question_type in ("text", "textarea", "email", "tel", "number", "url"):
                field = elements[0]
                try:
                    field.clear()
                    field.send_keys(answer)
                    value = self.driver.execute_script(
                        "return arguments[0].value || arguments[0].innerText || '';",
                        field,
                    )
                    if self._normalized_answer(str(value)) != self._normalized_answer(answer):
                        if question["required"]:
                            unanswered.append(prompt)
                except Exception:
                    if question["required"]:
                        unanswered.append(prompt)
            elif question["required"]:
                unanswered.append(prompt)

        return unanswered

    @staticmethod
    def _normalized_answer(answer: str) -> str:
        return " ".join(answer.casefold().split())

    def _submit_application(self) -> None:
        def find_submit_button(driver):
            for selector in (
                "[data-test='JobApplicationModal--SubmitButton']",
                "button[type='submit']",
                "button, [role='button']",
            ):
                for candidate in driver.find_elements(By.CSS_SELECTOR, selector):
                    if not candidate.is_displayed() or not candidate.is_enabled():
                        continue
                    label = " ".join(candidate.text.casefold().split())
                    if (
                        selector == "[data-test='JobApplicationModal--SubmitButton']"
                        or selector == "button[type='submit']"
                        or "submit" in label
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
            try:
                return "submitted" if not button.is_displayed() else False
            except StaleElementReferenceException:
                return "submitted"

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
        description = ""
        cover_letter = ""
        provider_used = ""
        status = "Failed"
        error_message = ""
        try:
            self.driver.get(link)
            description = self._job_description()
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
            self._wait_for_cover_letter_field().send_keys(cover_letter)
            unanswered = self._answer_application_questions(description)
            if unanswered:
                questions = "; ".join(unanswered[:5])
                raise RuntimeError(
                    "Could not enter answers for required questions from the saved profile or resume: "
                    f"{questions}"
                )
            if self.auto_submit:
                self._submit_application()
                status = "Submitted"
            else:
                status = "Drafted (not submitted)"
        except Exception as error:
            error_message = str(error).strip() or type(error).__name__

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
        finally:
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
        finally:
            self.driver.quit()
