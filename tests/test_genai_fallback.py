import tempfile
import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from google.genai import errors

from autoapply.helper.genai import CoverLetterGenerator
from autoapply.helper.gotolink import Gotolink
from autoapply.web import configuration_errors


VALID_LETTER = (
    "Dear Hiring Team,\n\nI am applying for this role. My resume includes "
    "relevant project work and technical skills that match the position. "
    "Thank you for considering my application."
)


def make_generator(gemini_key="gemini-key", deepseek_key="deepseek-key"):
    generator = CoverLetterGenerator.__new__(CoverLetterGenerator)
    generator.model_name = "gemini-3.8-flash"
    generator.gemini_api_key = gemini_key
    generator.api_key = gemini_key
    generator.deepseek_api_key = deepseek_key
    generator.resume_path = ""
    generator.progress_callback = None
    generator.client = object() if gemini_key else None
    generator.generation_config = object()
    generator.pdf_handler = None
    generator.last_generation_result = None
    generator._resume_context = lambda resume=None: (
        resume if isinstance(resume, str) else "Skills\nPython, data analysis",
        "",
    )
    return generator


class CoverLetterFallbackTests(unittest.TestCase):
    def test_gemini_success(self):
        generator = make_generator()
        with patch.object(generator, "_call_provider", return_value=VALID_LETTER) as call:
            result = generator.generate_cover_letter_result(
                {"role": "Engineer", "company": "Example", "description": "Build software."}
            )
        self.assertEqual(result["provider_used"], "gemini")
        self.assertEqual(result["cover_letter"], VALID_LETTER)
        self.assertTrue(result["success"])
        self.assertFalse(result["fallback_used"])
        call.assert_called_once()

    def test_gemini_fails_and_deepseek_succeeds(self):
        generator = make_generator()
        with patch.object(
            generator,
            "_call_provider",
            side_effect=[RuntimeError("unauthorized"), VALID_LETTER],
        ) as call:
            result = generator.generate_cover_letter_result("Build software.")
        self.assertEqual(result["provider_used"], "deepseek")
        self.assertTrue(result["fallback_used"])
        self.assertEqual(call.call_args_list[0].args[0], "gemini")
        self.assertEqual(call.call_args_list[1].args[0], "deepseek")

    def test_gemini_fails_and_missing_deepseek_uses_template(self):
        generator = make_generator(deepseek_key="")
        with patch.object(generator, "_call_provider", side_effect=RuntimeError("unavailable")) as call:
            result = generator.generate_cover_letter_result(
                {"role": "Engineer", "company": "Example"}
            )
        self.assertEqual(result["provider_used"], "template")
        self.assertTrue(result["fallback_used"])
        call.assert_called_once()

    def test_missing_gemini_key_uses_deepseek(self):
        generator = make_generator(gemini_key="")
        with patch.object(generator, "_call_provider", return_value=VALID_LETTER) as call:
            result = generator.generate_cover_letter_result("Build software.")
        self.assertEqual(result["provider_used"], "deepseek")
        self.assertTrue(result["fallback_used"])
        call.assert_called_once_with("deepseek", unittest.mock.ANY, generator.generation_config)

    def test_no_keys_uses_template_offline(self):
        generator = make_generator(gemini_key="", deepseek_key="")
        with patch.object(generator, "_call_provider") as call:
            result = generator.generate_cover_letter_result(
                {"role": "Analyst", "company": "Example", "description": "Analyze data."}
            )
        self.assertEqual(result["provider_used"], "template")
        self.assertTrue(result["success"])
        call.assert_not_called()

    def test_generator_can_initialize_without_any_api_key(self):
        with patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": "",
                "API_KEY": "",
                "DEEPSEEK_API_KEY": "",
                "RESUME_PATH": "",
            },
            clear=False,
        ):
            generator = CoverLetterGenerator(api_key="", deepseek_api_key="", resume_path="")
        self.assertIsNone(generator.client)
        self.assertEqual(generator.gemini_api_key, "")
        self.assertEqual(generator.deepseek_api_key, "")
        with patch("autoapply.helper.genai.urllib.request.urlopen") as urlopen:
            result = generator.generate_cover_letter_result(
                {"role": "Engineer", "company": "Example"}
            )
        self.assertEqual(result["provider_used"], "template")
        urlopen.assert_not_called()

    def test_dashboard_does_not_require_ai_keys_or_resume_path(self):
        with tempfile.TemporaryDirectory() as profile:
            self.assertEqual(
                configuration_errors({
                    "api_key": "",
                    "deepseek_api_key": "",
                    "profile_path": profile,
                    "resume_path": "",
                }),
                [],
            )

    def test_both_providers_failing_uses_template(self):
        generator = make_generator()
        with patch.object(generator, "_call_provider", side_effect=RuntimeError("unavailable")):
            result = generator.generate_cover_letter_result("Build software.")
        self.assertEqual(result["provider_used"], "template")
        self.assertEqual(len(result["error_details"]), 2)

    def test_both_providers_timing_out_uses_template(self):
        generator = make_generator()
        with patch.object(generator, "_call_provider", side_effect=TimeoutError()), patch(
            "autoapply.helper.genai.time.sleep"
        ):
            result = generator.generate_cover_letter_result("Build software.")
        self.assertEqual(result["provider_used"], "template")

    def test_gemini_503_retries_once_then_falls_back_to_deepseek(self):
        generator = make_generator()
        unavailable = errors.ServerError(
            503,
            {"error": {"status": "UNAVAILABLE", "message": "Service is busy"}},
        )
        with patch.object(
            generator,
            "_call_provider",
            side_effect=[unavailable, unavailable, VALID_LETTER],
        ) as call, patch("autoapply.helper.genai.time.sleep"):
            result = generator.generate_cover_letter_result("Build software.")
        self.assertEqual(result["provider_used"], "deepseek")
        self.assertEqual(call.call_count, 3)

    def test_deepseek_uses_openai_compatible_endpoint_and_auth_header(self):
        generator = make_generator(gemini_key="")
        response_body = b'{"choices":[{"message":{"content":"A valid response."}}]}'
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return response_body

        response = FakeResponse()
        with patch("autoapply.helper.genai.urllib.request.urlopen", return_value=response) as urlopen:
            answer = generator._call_deepseek("Prompt", SimpleNamespace(max_output_tokens=128))
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, CoverLetterGenerator.DEEPSEEK_ENDPOINT)
        self.assertEqual(request.get_header("Authorization"), "Bearer deepseek-key")
        self.assertEqual(answer, "A valid response.")

    def test_invalid_or_empty_provider_content_falls_back(self):
        generator = make_generator()
        with patch.object(generator, "_call_provider", return_value=""):
            result = generator.generate_cover_letter_result("Build software.")
        self.assertEqual(result["provider_used"], "template")
        self.assertTrue(result["fallback_used"])

    def test_template_handles_missing_resume_and_job_fields(self):
        generator = make_generator(gemini_key="", deepseek_key="")
        generator._resume_context = lambda resume=None: ("", "")
        result = generator.generate_cover_letter_result({})
        self.assertEqual(result["provider_used"], "template")
        self.assertTrue(result["cover_letter"])
        self.assertIn("the opportunity", result["cover_letter"])

    def test_missing_resume_file_uses_template_without_raising(self):
        generator = make_generator(gemini_key="", deepseek_key="")
        generator.resume_path = str(Path(tempfile.gettempdir()) / "missing-autowell-resume.pdf")
        generator.pdf_handler = SimpleNamespace(
            extract_links=Mock(side_effect=FileNotFoundError()),
            extract_text=Mock(),
        )
        result = generator.generate_cover_letter_result(
            {"role": "Engineer", "company": "Example"}
        )
        self.assertEqual(result["provider_used"], "template")
        self.assertTrue(result["cover_letter"])

    def test_fallback_does_not_repeat_application_submission(self):
        processor = Gotolink.__new__(Gotolink)
        processor.driver = SimpleNamespace(get=Mock())
        processor.cover_letter_generator = SimpleNamespace(
            generate_cover_letter_result=Mock(return_value={
                "cover_letter": VALID_LETTER,
                "provider_used": "template",
                "success": True,
                "fallback_used": True,
                "error_details": ["Gemini: HTTP 503"],
            })
        )
        processor.progress_callback = None
        processor.profile_answers = {}
        processor.auto_submit = True
        processor.generation_provider_counts = {}
        processor._job_description = Mock(return_value="Build software.")
        processor._click_apply = Mock()
        field = SimpleNamespace(send_keys=Mock())
        processor._wait_for_cover_letter_field = Mock(return_value=field)
        processor._answer_application_questions = Mock(return_value=[])
        processor._submit_application = Mock()
        processor._report = Mock()
        with tempfile.TemporaryDirectory() as directory:
            from autoapply.helper.excel_handler import ExcelHandler

            processor.excel_handler = ExcelHandler(str(Path(directory) / "applications.xlsx"))
            submitted, error = processor.process_link(
                "https://wellfound.com/jobs/test",
                {"role": "Engineer", "company": "Example"},
            )
        self.assertTrue(submitted)
        self.assertIsNone(error)
        processor._submit_application.assert_called_once()
        processor.cover_letter_generator.generate_cover_letter_result.assert_called_once()
        self.assertEqual(processor.generation_provider_counts, {"template": 1})

    def test_wellfound_submission_failure_is_not_reported_as_success(self):
        processor = Gotolink.__new__(Gotolink)
        processor.driver = SimpleNamespace(get=Mock())
        processor.cover_letter_generator = SimpleNamespace(
            generate_cover_letter_result=Mock(return_value={
                "cover_letter": VALID_LETTER,
                "provider_used": "template",
            })
        )
        processor.progress_callback = None
        processor.profile_answers = {}
        processor.auto_submit = True
        processor.generation_provider_counts = {}
        processor._job_description = Mock(return_value="Build software.")
        processor._click_apply = Mock()
        processor._wait_for_cover_letter_field = Mock(
            return_value=SimpleNamespace(send_keys=Mock())
        )
        processor._answer_application_questions = Mock(return_value=[])
        processor._submit_application = Mock(side_effect=RuntimeError("Wellfound rejected it"))
        processor._report = Mock()
        with tempfile.TemporaryDirectory() as directory:
            from autoapply.helper.excel_handler import ExcelHandler

            processor.excel_handler = ExcelHandler(str(Path(directory) / "applications.xlsx"))
            submitted, error = processor.process_link("https://wellfound.com/jobs/test")
            row = processor.excel_handler.sheet.iter_rows(min_row=2, values_only=True).__next__()
        self.assertFalse(submitted)
        self.assertIn("Wellfound rejected it", error)
        self.assertEqual(row[4], "Failed")
        self.assertEqual(row[6], "template")
        processor._submit_application.assert_called_once()


if __name__ == "__main__":
    unittest.main()
