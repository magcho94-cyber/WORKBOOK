import base64
import io
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import app
from workbook_core import make_pdfs, reading_ranges, source_from_text, validate


class WorkbookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.example = json.loads((Path(__file__).parent / "example.json").read_text())

    def test_source_input_preserves_english_and_checks_alignment(self):
        source = source_from_text("Test", "# Intro\nOne sentence.\nAnother sentence.\n\nLast sentence.",
                                  "첫 문장.\n두 번째 문장.\n\n마지막 문장.")
        self.assertEqual([len(p["sentences"]) for p in source["paragraphs"]], [2, 1])
        self.assertEqual(source["paragraphs"][0]["sentences"][0]["en"], "One sentence.")
        with self.assertRaises(ValueError):
            source_from_text("Test", "One.\nTwo.", "하나.")

    def test_invalid_grammar_target_is_rejected(self):
        draft = json.loads(json.dumps(self.example))
        draft["paragraphs"][0]["sentences"][1]["grammar_correct"] = "absent"
        with self.assertRaisesRegex(ValueError, "어법"):
            validate(draft)

    def test_reading_pages_are_two_or_three_sentences_when_possible(self):
        self.assertEqual(reading_ranges(1), [(0, 1)])
        self.assertEqual(reading_ranges(3), [(0, 3)])
        self.assertEqual(reading_ranges(5), [(0, 3), (3, 5)])
        self.assertEqual(reading_ranges(11), [(0, 3), (3, 5), (5, 7), (7, 9), (9, 11)])
        for size in range(2, 30):
            groups = reading_ranges(size)
            self.assertEqual([n for start, end in groups for n in range(start, end)], list(range(size)))
            self.assertTrue(all(end - start in (2, 3) for start, end in groups))

    def test_mocked_ai_does_not_rewrite_source_english(self):
        source = {"title": self.example["title"], "paragraphs": [{"heading": "Introduction",
            "sentences": [{"en": row["en"], "ko": row["ko"]} for row in self.example["paragraphs"][0]["sentences"]]}]}
        par = self.example["paragraphs"][0]
        mock_output = {"topic": par["topic"], "paragraph_title": par["paragraph_title"],
            "questions": par["questions"], "ox": par["ox"],
            "sentences": [{"translation": row["ko"], "blanks": row["blanks"],
                "grammar_correct": row["grammar_correct"], "grammar_wrong": row["grammar_wrong"],
                "grammar_reason": row["grammar_reason"], "connection": row["connection"]}
                for row in par["sentences"]]}
        with patch.object(app, "_generate_one", return_value=mock_output):
            result = app.create_draft(source, "gpt-5-mini")
        self.assertEqual([s["en"] for s in result["paragraphs"][0]["sentences"]],
                         [s["en"] for s in source["paragraphs"][0]["sentences"]])

    def test_all_stage_coverage_and_pdf_content(self):
        import fitz
        with tempfile.TemporaryDirectory() as tmp:
            student, teacher, manifest = make_pdfs(self.example, Path(tmp))
            self.assertEqual(set(manifest["coverage"].values()), {4})
            self.assertEqual(len(fitz.open(student)), 11)
            student_text = " ".join(page.get_text().replace("\n", " ") for page in fitz.open(student))
            teacher_text = " ".join(page.get_text().replace("\n", " ") for page in fitz.open(teacher))
            self.assertIn("문장 사이의 논리 관계 파악", student_text)
            self.assertIn("문장 연결", teacher_text)
            self.assertNotIn("\x00", teacher_text)

    def test_automatic_illustrations_are_embedded_and_cached(self):
        import fitz
        image = (Path(__file__).parent / "assets" / "jongrom-logo.png").read_bytes()
        response = {"data": [{"b64_json": base64.b64encode(image).decode()}]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, "OUTPUT", Path(tmp)):
            progress = []
            with patch.object(app, "_api_post", return_value=response) as api_call:
                result = app.render_zip(self.example, include_illustrations=True,
                                        progress=lambda done, total: progress.append((done, total)))
                self.assertEqual(api_call.call_count, 2)
                app.render_zip(self.example, include_illustrations=True)
                self.assertEqual(api_call.call_count, 2)
            self.assertEqual(progress, [(1, 2), (2, 2)])
            with zipfile.ZipFile(io.BytesIO(result)) as z:
                student = fitz.open(stream=z.read("학생용_워크북.pdf"), filetype="pdf")
                manifest = json.loads(z.read("문장_검증.json"))
                self.assertEqual(manifest["illustrations"], 2)
                logo_only = len(student[0].get_images())
                self.assertGreater(len(student[1].get_images()), logo_only)
                self.assertGreater(len(student[2].get_images()), logo_only)
                self.assertTrue(all(len(page.get_images()) == logo_only for page in list(student)[3:]))

    def test_odd_paragraph_illustrations_match_page_sentence_groups(self):
        import fitz
        draft = json.loads(json.dumps(self.example))
        last = dict(draft["paragraphs"][0]["sentences"][-1])
        last.update(en="After the war, the team saved even more works of art.",
                    ko="전쟁이 끝난 뒤에도 그 팀은 더 많은 예술 작품을 구했다.",
                    blanks=["team", "saved"], grammar_correct="saved", grammar_wrong="saving",
                    grammar_reason="시제가 과거이므로 saved가 알맞다.", connection="앞 문장의 활동 이후를 덧붙인다.")
        draft["paragraphs"][0]["sentences"].append(last)
        image = (Path(__file__).parent / "assets" / "jongrom-logo.png").read_bytes()
        response = {"data": [{"b64_json": base64.b64encode(image).decode()}]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, "OUTPUT", Path(tmp)), \
                patch.object(app, "_api_post", return_value=response):
            result = app.render_zip(draft, include_illustrations=True)
            self.assertEqual([len(group) for _, _, group in app.illustration_groups(draft)], [3, 2])
            with zipfile.ZipFile(io.BytesIO(result)) as z:
                student = fitz.open(stream=z.read("학생용_워크북.pdf"), filetype="pdf")
                first, second = student[1].get_text(), student[2].get_text()
                self.assertIn("1-03", first)
                self.assertNotIn("1-04", first)
                self.assertIn("1-04", second)
                self.assertIn("1-05", second)
                self.assertGreater(len(student[1].get_images()), len(student[0].get_images()))
                self.assertGreater(len(student[2].get_images()), len(student[0].get_images()))

    def test_render_job_reports_completion(self):
        image = (Path(__file__).parent / "assets" / "jongrom-logo.png").read_bytes()
        response = {"data": [{"b64_json": base64.b64encode(image).decode()}]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, "OUTPUT", Path(tmp)), \
                patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}), \
                patch.object(app, "_api_post", return_value=response):
            job_id = app.start_render_job(self.example)
            for _ in range(80):
                job = app.get_job(job_id)
                if job["status"] != "working":
                    break
                time.sleep(.05)
            self.assertEqual(job["status"], "done", job.get("error"))
            self.assertEqual(job["completed"], 2)
            self.assertTrue(app.JOBS[job_id]["payload"].startswith(b"PK"))

    def test_offline_print_option_skips_image_api(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, "OUTPUT", Path(tmp)), \
                patch.object(app, "_api_post") as api_call:
            result = app.render_zip(self.example, include_illustrations=False)
            api_call.assert_not_called()
            with zipfile.ZipFile(io.BytesIO(result)) as z:
                self.assertEqual(json.loads(z.read("문장_검증.json"))["illustrations"], 0)


if __name__ == "__main__":
    unittest.main()
