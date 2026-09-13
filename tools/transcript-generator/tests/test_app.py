import copy
import shutil
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError

from fastapi import HTTPException

import app as module


TRANSCRIPT = "The network recognizes handwritten digits. It uses 784 pixel values as inputs. The correct label is a digit from zero to nine."
VALID_QUESTION = {
    "section": "Science and Technology",
    "topic_key": "handwritten digit recognition",
    "question": "What task does the network perform?",
    "options": ["Translate text", "Recognize handwritten digits", "Predict weather", "Sort names"],
    "correct_answer": 1,
    "explanation": "The network recognizes handwritten digits.",
    "source_evidence": "The network recognizes handwritten digits.",
    "validation_override": False,
}


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.production = self.root / "melvin-questions.js"
        self.base = self.root / "reviewer-questions.js"
        shutil.copy2(Path(module.__file__).parents[2] / "melvin-questions.js", self.production)
        shutil.copy2(Path(module.__file__).parents[2] / "reviewer-questions.js", self.base)
        self.original_bank = self.production.read_bytes()
        self.originals = {name: getattr(module, name) for name in [
            "PROJECT_DIR", "GENERATED_DIR", "TRANSCRIPT_DIR", "QUESTION_DIR", "BACKUP_DIR",
            "PRODUCTION_BANK", "BASE_QUESTIONS", "run_question_bank_check", "call_ollama",
            "ollama_json", "urlopen",
        ]}
        module.PROJECT_DIR = self.root
        module.GENERATED_DIR = self.root / "generated"
        module.TRANSCRIPT_DIR = module.GENERATED_DIR / "transcripts"
        module.QUESTION_DIR = module.GENERATED_DIR / "questions"
        module.BACKUP_DIR = module.GENERATED_DIR / "backups"
        module.PRODUCTION_BANK = self.production
        module.BASE_QUESTIONS = self.base

    def tearDown(self):
        for name, value in self.originals.items():
            setattr(module, name, value)
        self.temp.cleanup()

    def question(self, **changes):
        value = copy.deepcopy(VALID_QUESTION)
        value.update(changes)
        return value

    def draft(self, questions=None, **changes):
        values = {
            "speaker": "Melvin", "session_number": "99", "transcript_type": "GenEd",
            "bank_name": "Melvin Session 99 - GenEd", "prefix": "melvin-s99",
            "transcript": TRANSCRIPT, "questions": questions or [self.question()],
        }
        values.update(changes)
        return module.DraftRequest(**values)

    def approval(self, **changes):
        values = self.draft().model_dump()
        values["confirmed"] = True
        values.update(changes)
        return module.ApproveRequest(**values)

    def validator_result(self, **changes):
        value = {"index": 0, "source_supported": True, "answer_supported": True, "explanation_supported": True, "section_correct": True, "unique_concept": True, "single_correct_answer": True, "unsupported_expansion": False, "suggested_section": "Science and Technology", "issues": []}
        value.update(changes)
        return value

    def test_deterministic_validation_accepts_valid_draft(self):
        report = module.validation_report("Melvin Session 99 - GenEd", "melvin-s99", [self.question()], TRANSCRIPT)
        self.assertTrue(report["valid"], report)
        self.assertEqual(report["generated_ids"], ["melvin-s99-1"])

    def test_deterministic_validation_finds_schema_and_duplicate_errors(self):
        broken = self.question(section="", question="", options=["A", "", "C", "D"], correct_answer=7, explanation="", source_evidence="")
        report = module.validation_report("New Bank", "new-prefix", [broken], TRANSCRIPT)
        codes = {error["code"] for error in report["errors"]}
        self.assertTrue({"SECTION_REQUIRED", "QUESTION_REQUIRED", "BLANK_OPTION", "ANSWER_RANGE", "EXPLANATION_REQUIRED", "SOURCE_EVIDENCE_REQUIRED"}.issubset(codes))

    def test_duplicate_topic_key_detection(self):
        second = self.question(question="Which input is processed?", topic_key="Handwritten--digit recognition")
        errors = module.question_errors([self.question(), second], TRANSCRIPT)
        self.assertIn("CONCEPT_DUPLICATE", {error["code"] for error in errors})

    def test_conceptual_duplicate_with_different_wording(self):
        second = self.question(topic_key="digit classifier", question="Which task is completed by the model?", source_evidence="The network recognizes handwritten digits.")
        errors = module.question_errors([self.question(), second], TRANSCRIPT)
        self.assertIn("CONCEPT_DUPLICATE", {error["code"] for error in errors})

    def test_invalid_section_detection(self):
        errors = module.question_errors([self.question(section="Computer Science")], TRANSCRIPT)
        self.assertIn("INVALID_SECTION", {error["code"] for error in errors})

    def test_suggested_section_handling(self):
        payload = {"results": [self.validator_result(section_correct=False, suggested_section="Science and Technology", issues=["Technical concept is miscategorized."])]}
        result = module.parse_validator_results(payload, [self.question(section="Understanding the Self")])[0]
        self.assertEqual(result["status"], "FLAGGED")
        self.assertEqual(result["suggested_section"], "Science and Technology")

    def test_unsupported_explanation_detection(self):
        errors = module.question_errors([self.question(explanation="Photosynthesis in chloroplasts produces glucose using sunlight and carbon dioxide.")], TRANSCRIPT)
        self.assertIn("EXPLANATION_GROUNDING", {error["code"] for error in errors})

    def test_source_evidence_requirement(self):
        errors = module.question_errors([self.question(source_evidence="")], TRANSCRIPT)
        self.assertIn("SOURCE_EVIDENCE_REQUIRED", {error["code"] for error in errors})

    def test_duplicate_option_detection(self):
        errors = module.question_errors([self.question(options=["Same", "Same", "Other", "Last"])], TRANSCRIPT)
        self.assertIn("DUPLICATE_OPTIONS", {error["code"] for error in errors})

    def test_answer_position_redistribution(self):
        questions = [self.question(topic_key=f"topic {index}", question=f"Question {index}?", correct_answer=0) for index in range(10)]
        redistributed = module.redistribute_answers(questions, "session-seed")
        counts = [sum(item["correct_answer"] == position for item in redistributed) for position in range(4)]
        self.assertLessEqual(max(counts) - min(counts), 1)

    def test_answer_index_integrity_after_option_shuffle(self):
        questions = [self.question(topic_key=f"topic {index}", question=f"Question {index}?") for index in range(8)]
        correct_texts = [item["options"][item["correct_answer"]] for item in questions]
        redistributed = module.redistribute_answers(questions, "integrity")
        self.assertEqual(correct_texts, [item["options"][item["correct_answer"]] for item in redistributed])

    def test_uneven_answer_distribution_is_rejected(self):
        questions = [self.question(topic_key=f"topic {index}", question=f"Question {index}?", correct_answer=0) for index in range(8)]
        report = module.validation_report("Unique Bank", "unique-prefix", questions, TRANSCRIPT)
        self.assertTrue(any("unevenly distributed" in message for message in report["general_errors"]))

    def test_validator_structured_output_parsing(self):
        result = module.parse_validator_results({"results": [self.validator_result()]}, [self.question()])[0]
        self.assertEqual(result["status"], "PASS")

    def test_malformed_validator_output_is_flagged(self):
        result = module.parse_validator_results({"results": [{"index": 0}]}, [self.question()])[0]
        self.assertEqual(result["status"], "FLAGGED")
        self.assertIn("malformed", result["issues"][0].lower())

    def test_insufficient_unique_topics(self):
        module.call_ollama = lambda *args, **kwargs: {"topics": [{"topic_key": "digit recognition", "source_evidence": "The network recognizes handwritten digits.", "suggested_section": "Science and Technology"}]}
        request = module.GenerateRequest(**self.draft().model_dump(exclude={"questions"}), question_count=2)
        with self.assertRaises(HTTPException) as raised:
            module.generate(request)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("Only 1 sufficiently distinct", raised.exception.detail["message"])

    def test_final_tuple_conversion_strips_internal_metadata(self):
        result = module.question_to_tuple(self.question(validation_override=True))
        self.assertEqual(len(result), 5)
        self.assertEqual(result, [VALID_QUESTION["section"], VALID_QUESTION["question"], VALID_QUESTION["options"], VALID_QUESTION["correct_answer"], VALID_QUESTION["explanation"]])

    def test_existing_bank_prefix_and_base_id_conflicts_are_rejected(self):
        conflict = module.validation_report("Melvin Session 8 - GenEd", "melvin-s8", [self.question()], TRANSCRIPT)
        self.assertFalse(conflict["valid"])
        base_id = module.validation_report("Unique", "mini", [self.question()], TRANSCRIPT)
        self.assertIn("Generated question IDs conflict with the base reviewer bank.", base_id["general_errors"])

    def test_save_edited_transcript_requires_overwrite(self):
        request = module.SaveRequest(session_number="99", transcript_type="GenEd", transcript="edited transcript")
        first = module.save_transcript(request)
        with self.assertRaises(HTTPException) as raised:
            module.save_transcript(request)
        self.assertEqual(raised.exception.status_code, 409)
        module.save_transcript(request.model_copy(update={"transcript": "new edit", "overwrite": True}))
        self.assertEqual((module.TRANSCRIPT_DIR / first["filename"]).read_text(encoding="utf-8"), "new edit")

    def test_generation_and_validation_leave_production_untouched(self):
        calls = 0
        def fake_call(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"topics": [{"topic_key": "handwritten digit recognition", "source_evidence": "The network recognizes handwritten digits.", "suggested_section": "Science and Technology"}]}
            return {"questions": [self.question()]}
        module.call_ollama = fake_call
        request = module.GenerateRequest(**self.draft().model_dump(exclude={"questions"}), question_count=1)
        result = module.generate(request)
        module.validate_questions(self.draft(questions=result["questions"]))
        self.assertEqual(self.production.read_bytes(), self.original_bank)

    def test_generation_uses_mocked_local_model_and_saves_draft(self):
        calls = 0
        def fake_call(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"topics": [{"topic_key": "handwritten digit recognition", "source_evidence": "The network recognizes handwritten digits.", "suggested_section": "Science and Technology"}]}
            return {"questions": [self.question()]}
        module.call_ollama = fake_call
        request = module.GenerateRequest(**self.draft().model_dump(exclude={"questions", "topic_pool"}), question_count=1)
        result = module.generate(request)
        self.assertTrue((self.root / result["draft"]).exists())
        self.assertEqual(result["questions"][0]["topic_key"], "handwritten digit recognition")
        self.assertEqual(self.production.read_bytes(), self.original_bank)

    def test_named_model_fields_are_converted_to_reviewer_tuple(self):
        internal = module.extract_questions_from_response({"questions": [self.question()]})[0]
        self.assertEqual(module.question_to_tuple(internal), [VALID_QUESTION["section"], VALID_QUESTION["question"], VALID_QUESTION["options"], VALID_QUESTION["correct_answer"], VALID_QUESTION["explanation"]])

    def test_unconfirmed_approval_does_not_modify_production(self):
        with self.assertRaises(HTTPException) as raised:
            module.approve_questions(self.approval(confirmed=False))
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(self.production.read_bytes(), self.original_bank)

    def test_approval_backs_up_appends_and_validates(self):
        result = module.approve_questions(self.approval())
        self.assertEqual(result["questions_added"], 1)
        self.assertEqual(result["total_questions"], 206)
        self.assertNotIn("topic_key", self.production.read_text(encoding="utf-8"))
        self.assertEqual((self.root / result["backup"]).read_bytes(), self.original_bank)

    def test_final_failure_restores_backup(self):
        calls = 0
        def fail_after_candidate(path):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"count": 206}
            raise ValueError("forced final failure")
        module.run_question_bank_check = fail_after_candidate
        with self.assertRaises(HTTPException) as raised:
            module.approve_questions(self.approval())
        self.assertIn("backup was restored", str(raised.exception.detail))
        self.assertEqual(self.production.read_bytes(), self.original_bank)

    def test_download_js_does_not_modify_production(self):
        response = module.download_js(self.draft())
        source = Path(response.path).read_text(encoding="utf-8")
        self.assertNotIn("topic_key", source)
        self.assertNotIn("source_evidence", source)
        self.assertEqual(self.production.read_bytes(), self.original_bank)

    def test_ollama_error_messages(self):
        module.urlopen = lambda *args, **kwargs: (_ for _ in ()).throw(URLError("offline"))
        with self.assertRaises(HTTPException) as raised:
            module.ollama_json("/api/tags")
        self.assertEqual(raised.exception.detail, "Ollama is not running. Start Ollama and try again.")
        module.ollama_json = lambda *args, **kwargs: {"models": [{"name": "other:latest"}]}
        with self.assertRaises(HTTPException) as missing:
            module.ensure_ollama_model("qwen3:8b")
        self.assertEqual(missing.exception.detail, "qwen3:8b is not installed. Run: ollama pull qwen3:8b")


if __name__ == "__main__":
    unittest.main()
