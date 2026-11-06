import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from selenium.common.exceptions import ElementNotInteractableException, TimeoutException

from autoapply.helper.application_answers import (
    match_profile_answer,
    match_saved_question,
    normalize_question,
)
from autoapply.helper.gotolink import Gotolink


class FakeField:
    def __init__(self, value="", selected=False, attributes=None):
        self.value = value
        self.selected = selected
        self.clicked = 0
        self.attributes = attributes or {}

    def clear(self):
        self.value = ""

    def send_keys(self, value):
        self.value = value

    def is_selected(self):
        return self.selected

    def click(self):
        self.clicked += 1
        self.selected = not self.selected

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def get_attribute(self, name):
        if name == "value":
            return self.value
        return self.attributes.get(name, "")


class FakeSelect:
    def __init__(self, field):
        self.field = field

    def select_by_visible_text(self, label):
        self.field.value = label

    def select_by_value(self, value):
        self.field.value = value


class FakeDriver:
    def __init__(self, fields=None):
        self.fields = fields or {}
        self.page_queries = []

    def find_elements(self, _by, selector):
        self.page_queries.append(selector)
        return self.fields.get(selector, [])

    def execute_script(self, script, *args):
        if "root.querySelectorAll('[data-autowell-question-id]')" in script:
            question_id = args[0]
            return self.fields.get(f'[data-autowell-question-id="{question_id}"]', [])
        if "const element = arguments[0]" in script:
            return True
        field = args[0] if args else None
        if "selectedOptions" in script:
            return {"value": field.value, "label": field.value}
        if "input.click" in script:
            field.selected = True
            return None
        if "arguments[0].checked" in script:
            return field.selected
        if "arguments[0].value || arguments[0].innerText" in script:
            return field.value
        return None


def make_processor():
    processor = Gotolink.__new__(Gotolink)
    processor.profile_answers = {}
    processor.question_answers = {}
    processor.progress_callback = None
    processor.question_callback = None
    processor.form_callback = None
    processor.cover_letter_generator = SimpleNamespace(
        get_resume_text=lambda: "",
        answer_application_questions=Mock(return_value=[]),
    )
    processor.driver = FakeDriver()
    processor.form_summary = {}
    processor.cover_letter_inserted = False
    processor.pause_for_review = False
    processor.dry_run = False
    return processor


class ApplicationQuestionMatchingTests(unittest.TestCase):
    def test_phone_number_variations_use_saved_phone(self):
        answers = {"phone": "+91 9876543210"}
        for question in ("Phone number", "Contact number", "Mobile number"):
            self.assertEqual(match_profile_answer(question, answers), ("phone", answers["phone"]))

    def test_visa_sponsorship_variations_use_saved_value(self):
        answers = {"visa_sponsorship": "No"}
        questions = (
            "Do you require visa sponsorship?",
            "Will you now or in the future require sponsorship?",
        )
        for question in questions:
            self.assertEqual(match_profile_answer(question, answers)[1], "No")

    def test_referral_status_and_referrer_details_are_distinguished(self):
        answers = {"referred": "Yes", "referral_details": "Ravi, ravi@example.com"}
        self.assertEqual(match_profile_answer("Were you referred by someone?", answers)[1], "Yes")
        self.assertEqual(match_profile_answer("Who referred you?", answers)[1], answers["referral_details"])
        self.assertEqual(
            Gotolink._profile_answer(
                SimpleNamespace(profile_answers={"referred": "No"}), "If yes, give the name of the person that referred you"
            ),
            "N/A",
        )

    def test_basic_application_section_maps_profile_answers(self):
        processor = make_processor()
        processor.profile_answers = {
            "phone": "+1 555 0100",
            "referred": "No",
            "visa_sponsorship": "No",
        }
        questions = [
            {"id": "0", "prompt": "Phone Number", "type": "tel", "required": True, "options": [], "value": ""},
            {"id": "1", "prompt": "Were you referred to Certa?", "type": "radio", "required": True,
             "options": [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}], "value": "", "checked": False},
            {"id": "2", "prompt": "If yes, please give the name and contact details of the person that referred you",
             "type": "textarea", "required": False, "options": [], "value": ""},
            {"id": "3", "prompt": "Will you now or in the future require sponsorship for employment visa status?",
             "type": "radio", "required": True, "options": [{"label": "Yes", "value": "yes"},
             {"label": "No", "value": "no"}], "value": "", "checked": False},
        ]
        processor._wait_for_application_fields = lambda: questions
        processor._application_questions = lambda: questions
        fields = {
            '[data-autowell-question-id="0"]': [FakeField()],
            '[data-autowell-question-id="1"]': [FakeField(), FakeField()],
            '[data-autowell-question-id="2"]': [FakeField()],
            '[data-autowell-question-id="3"]': [FakeField(), FakeField()],
        }
        processor.driver = FakeDriver(fields)
        self.assertEqual(processor._answer_application_questions("Role"), [])
        self.assertEqual(fields['[data-autowell-question-id="0"]'][0].value, "+1 555 0100")
        self.assertTrue(fields['[data-autowell-question-id="1"]'][1].selected)
        self.assertEqual(fields['[data-autowell-question-id="2"]'][0].value, "N/A")
        self.assertTrue(fields['[data-autowell-question-id="3"]'][1].selected)

    def test_screening_questions_use_configured_answers_not_ai_guesses(self):
        processor = make_processor()
        rest_question = "How would you rate your hands-on experience with REST APIs, JSON, webhooks, and data mapping?"
        llm_question = "Have you worked with LLMs/Generative AI in a production environment?"
        processor.profile_answers = {
            "work_authorization": "Yes",
            "experience": "0",
        }
        processor.question_answers = {
            normalize_question(rest_question): {
                "question": rest_question, "answer": "Moderate - I have working knowledge",
            },
            normalize_question(llm_question): {
                "question": llm_question, "answer": "Yes, but primarily in POCs/prototypes",
            },
        }
        questions = [
            {"id": "0", "prompt": "Are you legally authorized to work here?", "type": "radio",
             "required": True, "options": [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}],
             "value": "", "checked": False},
            {"id": "1", "prompt": "How many years of experience do you have in customer-facing technical delivery?",
             "type": "number", "required": True, "options": [], "value": ""},
            {"id": "2", "prompt": rest_question, "type": "radio", "required": True,
             "options": [{"label": "Strong - I work with these regularly", "value": "strong"},
                         {"label": "Moderate - I have working knowledge", "value": "moderate"},
                         {"label": "Limited / No hands-on experience", "value": "limited"}],
             "value": "", "checked": False},
            {"id": "3", "prompt": llm_question, "type": "radio", "required": True,
             "options": [{"label": "Yes, I have built/deployed LLM-powered solutions in production", "value": "production"},
                         {"label": "Yes, but primarily in POCs/prototypes", "value": "poc"},
                         {"label": "No", "value": "no"}],
             "value": "", "checked": False},
        ]
        processor._wait_for_application_fields = lambda: questions
        processor._application_questions = lambda: questions
        fields = {
            f'[data-autowell-question-id="{index}"]': [
                FakeField() for _ in question.get("options", [])
            ] or [FakeField()]
            for index, question in enumerate(questions)
        }
        processor.driver = FakeDriver(fields)
        self.assertEqual(processor._answer_application_questions("Role"), [])
        self.assertEqual(fields['[data-autowell-question-id="1"]'][0].value, "0")
        self.assertTrue(fields['[data-autowell-question-id="0"]'][0].selected)
        self.assertTrue(fields['[data-autowell-question-id="2"]'][1].selected)
        self.assertTrue(fields['[data-autowell-question-id="3"]'][1].selected)
        processor.cover_letter_generator.answer_application_questions.assert_not_called()

    def test_linkedin_and_github_questions_use_saved_urls(self):
        answers = {
            "linkedin": "https://linkedin.com/in/example",
            "github": "https://github.com/example",
        }
        self.assertEqual(match_profile_answer("LinkedIn profile URL", answers)[1], answers["linkedin"])
        self.assertEqual(match_profile_answer("GitHub profile", answers)[1], answers["github"])

    def test_saved_question_library_matches_normalized_variants(self):
        saved = {
            normalize_question("What is your notice period?"): {
                "question": "What is your notice period?",
                "answer": "Available immediately",
            }
        }
        question, answer = match_saved_question("Please share your notice period", saved)
        self.assertEqual(question, "What is your notice period?")
        self.assertEqual(answer, "Available immediately")

    def test_radio_selects_matching_saved_option(self):
        processor = make_processor()
        radios = [FakeField(), FakeField()]
        processor.driver = FakeDriver({
            '[data-autowell-question-id="2"]': radios,
        })
        question = {
            "id": "2",
            "type": "radio",
            "options": [
                {"label": "Yes", "value": "yes"},
                {"label": "No", "value": "no"},
            ],
        }
        with patch("autoapply.helper.gotolink.WebDriverWait") as wait:
            wait.return_value.until.side_effect = lambda condition: condition(None)
            self.assertTrue(processor._fill_form_field(question, "No"))
        self.assertFalse(radios[0].selected)
        self.assertTrue(radios[1].selected)

    def test_application_experience_uses_explicit_configured_years(self):
        answers = {"experience": "0"}
        question = (
            "How many years of experience do you have in customer-facing "
            "technical delivery?"
        )
        self.assertEqual(match_profile_answer(question, answers), ("experience", "0"))

    def test_dropdown_selects_matching_saved_option(self):
        processor = make_processor()
        field = FakeField()
        processor.driver = FakeDriver({'[data-autowell-question-id="1"]': [field]})
        question = {
            "id": "1",
            "type": "select",
            "options": [
                {"label": "Internship", "value": "intern"},
                {"label": "Full-time", "value": "full"},
            ],
        }
        with patch("autoapply.helper.gotolink.Select", FakeSelect):
            self.assertTrue(processor._fill_form_field(question, "Internship"))
        self.assertEqual(field.value, "Internship")

    def test_checkbox_state_is_set_to_saved_yes_answer(self):
        processor = make_processor()
        field = FakeField(selected=False)
        processor.driver = FakeDriver({'[data-autowell-question-id="3"]': [field]})
        question = {"id": "3", "type": "checkbox", "options": [{"label": "I agree"}]}
        self.assertTrue(processor._fill_form_field(question, "Yes"))
        self.assertTrue(field.selected)

    def test_global_search_input_is_never_used_as_an_application_field(self):
        application_field = FakeField(attributes={"id": "phone", "name": "phone", "type": "tel"})
        global_search = FakeField(
            value="existing search", attributes={"id": "search", "name": "search", "type": "text"}
        )
        processor = make_processor()
        processor.driver = FakeDriver({
            '[data-autowell-question-id="0"]': [application_field],
            "input": [global_search],
        })
        self.assertTrue(processor._fill_form_field(
            {"id": "0", "type": "tel"}, "+1 555 0100"
        ))
        self.assertEqual(application_field.value, "+1 555 0100")
        self.assertEqual(global_search.value, "existing search")
        self.assertNotIn("input", processor.driver.page_queries)

    def test_field_interaction_failure_does_not_abort_other_fields(self):
        processor = make_processor()
        processor.profile_answers = {"phone": "555-0100"}
        processor.question_answers = {
            normalize_question("Current location"): {
                "question": "Current location", "answer": "Remote",
            }
        }
        questions = [
            {"id": "0", "prompt": "Phone number", "type": "tel", "required": True,
             "options": [], "value": ""},
            {"id": "1", "prompt": "Current location", "type": "text", "required": False,
             "options": [], "value": ""},
        ]
        processor._wait_for_application_fields = lambda: questions
        processor._application_questions = lambda: questions
        failed_field = FakeField(attributes={"id": "phone", "type": "tel"})
        failed_field.clear = Mock(side_effect=ElementNotInteractableException("offscreen"))
        other_field = FakeField(attributes={"id": "location", "type": "text"})
        processor.driver = FakeDriver({
            '[data-autowell-question-id="0"]': [failed_field],
            '[data-autowell-question-id="1"]': [other_field],
        })
        processor._save_application_form_debug_artifacts = Mock()
        self.assertEqual(processor._answer_application_questions("Role"), ["Phone number"])
        self.assertEqual(other_field.value, "Remote")
        self.assertTrue(processor.pause_for_review)
        processor._save_application_form_debug_artifacts.assert_called_once()

    def test_optional_field_interaction_failure_still_pauses_submission(self):
        processor = make_processor()
        question = {
            "id": "0", "prompt": "Additional details", "type": "textarea",
            "required": False, "options": [], "value": "",
        }
        processor.question_answers = {
            normalize_question("Additional details"): {
                "question": "Additional details", "answer": "My response",
            }
        }
        processor._wait_for_application_fields = lambda: [question]
        processor._application_questions = lambda: [question]
        field = FakeField(attributes={"id": "details", "type": "textarea"})
        field.clear = Mock(side_effect=ElementNotInteractableException("offscreen"))
        processor.driver = FakeDriver({'[data-autowell-question-id="0"]': [field]})
        processor._save_application_form_debug_artifacts = Mock()
        self.assertEqual(processor._answer_application_questions("Role"), [])
        self.assertTrue(processor.pause_for_review)
        self.assertEqual(processor.form_summary["validation"], "needs review")

    def test_review_pause_keeps_browser_open(self):
        processor = make_processor()
        processor.driver.quit = Mock()
        processor.process_link = Mock(
            side_effect=lambda *_: (
                setattr(processor, "pause_for_review", True) or (False, "Needs review")
            )
        )
        processor.process_selected_jobs([{"link": "https://wellfound.com/jobs/test"}])
        processor.driver.quit.assert_not_called()

    def test_submit_requires_explicit_wellfound_confirmation(self):
        processor = make_processor()
        button = SimpleNamespace(
            text="Send application",
            is_displayed=lambda: True,
            is_enabled=lambda: True,
            get_attribute=lambda _name: "",
            click=Mock(),
        )
        root = SimpleNamespace(find_elements=lambda *_args: [button])
        body = SimpleNamespace(text="Job details")
        processor.driver = SimpleNamespace(
            execute_script=lambda *_args: root,
            find_element=lambda *_args: body,
        )
        with patch("autoapply.helper.gotolink.WebDriverWait") as wait:
            wait.return_value.until.side_effect = [button, TimeoutException()]
            with self.assertRaisesRegex(RuntimeError, "did not confirm"):
                processor._submit_application()
        button.click.assert_called_once()

    def test_cover_letter_detection_requires_explicit_cover_letter_label(self):
        self.assertTrue(processor_prompt("Cover Letter"))
        self.assertFalse(processor_prompt("Additional information"))
        self.assertFalse(processor_prompt("Why are you interested in this role?"))

    def test_cover_letter_is_never_put_in_arbitrary_textarea(self):
        processor = make_processor()
        field = FakeField()
        processor._application_questions = lambda: [{
            "id": "0", "type": "textarea", "cover_letter": False,
        }]
        processor.driver = FakeDriver({'[data-autowell-question-id="0"]': [field]})
        self.assertIsNone(processor._cover_letter_field())
        self.assertFalse(processor._insert_cover_letter_if_present("Generated letter"))
        self.assertEqual(field.value, "")

    def test_unknown_required_question_is_flagged_for_manual_review(self):
        processor = make_processor()
        question = {
            "id": "0", "prompt": "Do you have another preference?",
            "label": "Do you have another preference?", "type": "text",
            "required": True, "options": [], "value": "", "cover_letter": False,
        }
        processor._wait_for_application_fields = lambda: [question]
        processor._application_questions = lambda: [question]
        processor.question_callback = Mock()
        processor.form_callback = Mock()
        result = processor._answer_application_questions("A role.")
        self.assertEqual(result, ["Do you have another preference?"])
        self.assertTrue(processor.pause_for_review)
        processor.question_callback.assert_called_once()
        processor.form_callback.assert_called_once()

    def test_dry_run_reports_proposed_answer_without_mutating_field(self):
        processor = make_processor()
        processor.dry_run = True
        processor.profile_answers = {"phone": "555-0100"}
        question = {
            "id": "0", "prompt": "Phone Number", "label": "Phone Number",
            "type": "tel", "required": True, "options": [], "value": "",
        }
        processor._wait_for_application_fields = lambda: [question]
        processor._application_questions = lambda: [question]
        processor._report = Mock()
        field = FakeField()
        processor.driver = FakeDriver({'[data-autowell-question-id="0"]': [field]})
        self.assertEqual(processor._answer_application_questions("Role"), [])
        self.assertEqual(field.value, "")
        self.assertEqual(processor.form_summary["proposed"], 1)
        self.assertEqual(processor.form_summary["answered"], 0)
        self.assertTrue(any("555-0100" in call.args[0] for call in processor._report.call_args_list))
        processor.cover_letter_generator.answer_application_questions.assert_not_called()

    def test_dry_run_process_never_generates_or_submits_application(self):
        processor = make_processor()
        processor.dry_run = True
        processor.auto_submit = True
        processor.driver = SimpleNamespace(get=Mock())
        processor.cover_letter_generator.generate_cover_letter_result = Mock()
        processor._job_description = Mock(return_value="Role description")
        processor._click_apply = Mock()
        processor._wait_for_application_fields = Mock(return_value=[])
        processor._cover_letter_field = Mock(return_value=None)
        processor._answer_application_questions = Mock(return_value=[])
        processor._submit_application = Mock()
        processor._report = Mock()
        with patch("autoapply.helper.excel_handler.ExcelHandler") as excel_class:
            processor.excel_handler = excel_class.return_value
            submitted, error = processor.process_link("https://wellfound.com/jobs/test")
        self.assertFalse(submitted)
        self.assertIsNone(error)
        processor.cover_letter_generator.generate_cover_letter_result.assert_not_called()
        processor._submit_application.assert_not_called()
        self.assertIn("Dry run", excel_class.return_value.add_application.call_args.args[3])

    def test_sensitive_question_is_not_sent_to_ai_without_profile_answer(self):
        processor = make_processor()
        question = {
            "id": "0", "prompt": "Phone number", "label": "Phone number",
            "type": "tel", "required": True, "options": [], "value": "",
        }
        processor._wait_for_application_fields = lambda: [question]
        processor._application_questions = lambda: [question]
        self.assertEqual(processor._answer_application_questions("Role"), ["Phone number"])
        processor.cover_letter_generator.answer_application_questions.assert_not_called()

    def test_missing_profile_answer_does_not_create_personal_fact(self):
        processor = make_processor()
        self.assertEqual(processor._profile_answer("Do you require visa sponsorship?"), "")
        self.assertEqual(processor._profile_answer("Years of experience"), "")

    def test_work_authorization_question_is_not_generated_by_ai(self):
        processor = make_processor()
        question = {
            "id": "0", "prompt": "Are you legally authorized to work here?",
            "label": "Are you legally authorized to work here?", "type": "radio",
            "required": True, "options": [{"label": "Yes", "value": "yes"}],
            "value": "", "checked": False,
        }
        processor._wait_for_application_fields = lambda: [question]
        processor._application_questions = lambda: [question]
        self.assertEqual(
            processor._answer_application_questions("Role"),
            ["Are you legally authorized to work here?"],
        )
        processor.cover_letter_generator.answer_application_questions.assert_not_called()

    def test_resume_contact_phone_is_used_when_no_saved_phone_exists(self):
        processor = make_processor()
        question = {
            "id": "0", "prompt": "Phone number", "label": "Phone number",
            "type": "tel", "required": True, "options": [], "value": "",
        }
        processor.cover_letter_generator.get_resume_text = lambda: "Contact: +1 (555) 123-4567"
        processor._wait_for_application_fields = lambda: [question]
        processor._application_questions = lambda: [question]
        field = FakeField()
        processor.driver = FakeDriver({'[data-autowell-question-id="0"]': [field]})
        self.assertEqual(processor._answer_application_questions("Role"), [])
        self.assertEqual(field.value, "+1 (555) 123-4567")

    def test_dynamic_fields_are_rescanned_and_answered(self):
        processor = make_processor()
        first = {
            "id": "0", "prompt": "Phone number", "label": "Phone number",
            "type": "tel", "required": True, "options": [], "value": "",
        }
        second = {
            "id": "1", "prompt": "Preferred location", "label": "Preferred location",
            "type": "text", "required": True, "options": [], "value": "",
        }
        processor.profile_answers = {"phone": "555-0100"}
        processor.question_answers = {
            normalize_question("Preferred location"): {
                "question": "Preferred location", "answer": "Remote",
            }
        }
        processor._wait_for_application_fields = lambda: [first]
        processor._application_questions = Mock(side_effect=[[first, second], [first, second]])
        phone = FakeField()
        location = FakeField()
        processor.driver = FakeDriver({
            '[data-autowell-question-id="0"]': [phone],
            '[data-autowell-question-id="1"]': [location],
        })
        self.assertEqual(processor._answer_application_questions("Role"), [])
        self.assertEqual(phone.value, "555-0100")
        self.assertEqual(location.value, "Remote")

    def test_required_field_validation_reports_unfilled_controls(self):
        processor = make_processor()
        processor.driver.execute_script = lambda *_args: ["Phone number"]
        processor._application_questions = lambda: []
        self.assertEqual(processor._validate_application_form(), ["Phone number"])

    def test_multi_step_form_detects_next_step(self):
        processor = make_processor()
        button = SimpleNamespace(
            text="Continue",
            is_displayed=lambda: True,
            is_enabled=lambda: True,
            click=Mock(),
        )
        processor.driver = SimpleNamespace(
            execute_script=lambda *_args: [button],
            find_elements=lambda *_args: [button],
        )
        processor._application_questions = Mock(side_effect=[[], [{"prompt": "New question", "type": "text"}]])
        with patch("autoapply.helper.gotolink.WebDriverWait") as wait:
            wait.return_value.until.return_value = True
            self.assertTrue(processor._advance_application_step())
        button.click.assert_called_once()

    def test_scanner_scrolls_internal_container_and_restores_position(self):
        processor = make_processor()
        scroller = object()
        positions = iter([
            {"top": 100, "max": 200},
            {"top": 200, "max": 200},
        ])
        scripts = []

        def execute_script(script, *args):
            scripts.append(script)
            if "return scrollables[0]" in script:
                return scroller
            if "return arguments[0].scrollTop;" in script:
                return 0
            if "node.scrollTop = next" in script:
                return next(positions)
            return None

        processor.driver.execute_script = execute_script
        processor._application_questions = Mock(return_value=[])
        with patch("autoapply.helper.gotolink.sleep"):
            processor._scroll_application_container()
        self.assertEqual(processor._application_questions.call_count, 2)
        self.assertTrue(any("node.scrollTop = next" in script for script in scripts))
        self.assertTrue("arguments[0].scrollTop = arguments[1]" in scripts[-1])

    def test_detection_failure_collects_diagnostics_and_artifacts(self):
        processor = make_processor()
        processor._application_questions = Mock(return_value=[])
        processor._application_form_diagnostics = Mock(return_value={
            "modal_found": True,
            "raw_controls": 4,
            "visible_controls": 0,
            "counts": {"inputs": 2, "textareas": 1, "radios": 1},
        })
        processor._save_application_form_debug_artifacts = Mock()
        processor.progress_callback = Mock()
        with patch("autoapply.helper.gotolink.WebDriverWait") as wait:
            wait.return_value.until.side_effect = TimeoutException()
            with self.assertRaisesRegex(RuntimeError, "hidden, disabled, or unsupported"):
                processor._wait_for_application_fields()
        processor._save_application_form_debug_artifacts.assert_called_once()
        self.assertTrue(any(
            "raw controls=4" in call.args[0]
            for call in processor.progress_callback.call_args_list
        ))


def processor_prompt(prompt):
    return Gotolink._is_cover_letter_prompt(prompt)


if __name__ == "__main__":
    unittest.main()
