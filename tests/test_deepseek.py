from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pdfplumber
from pdfminer.pdfparser import PDFSyntaxError
from pdfplumber.utils.exceptions import PdfminerException
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from bookmarker.deepseek import (API_URL, DEEPSEEK_MODEL, PROMPT_VERSION,
                                 TOC_PROMPT_VERSION,
                                 DeepSeekClient, DeepSeekError, PageRenderer,
                                 VisionPageLabels,
                                 _discover_toc, _extract_toc, _parse_toc_entries,
                                 process_book_deepseek)


def scanned_page(number: int) -> Image.Image:
    """Draw text into pixels, so the resulting PDF has no searchable text layer."""
    image = Image.new("RGB", (420, 540), "white")
    drawing = ImageDraw.Draw(image)
    if number == 1:
        drawing.text((40, 50), "A Sample Book", fill="black")
    elif number == 2:
        drawing.text((40, 45), "Contents", fill="black")
        for index, text in enumerate(("Chapter 1 Introduction  1", "1.1 Scope  2",
                                      "Chapter 2 Methods  4", "2.1 Setup  6",
                                      "Appendix  7")):
            drawing.text((40, 90 + index * 35), text, fill="black")
    elif number == 3:
        drawing.text((40, 50), "Preface", fill="black")
    else:
        drawing.text((40, 50), f"Body page {number - 3}", fill="black")
        drawing.text((200, 510), str(number - 3), fill="black")
    return image


def make_scanned_book(path: Path) -> None:
    book = canvas.Canvas(str(path), pagesize=(612, 792))
    for number in range(1, 11):
        book.drawImage(ImageReader(scanned_page(number)), 0, 0, width=612, height=792)
        book.showPage()
    book.save()


def add_page_number_bookmarks(path: Path) -> None:
    reader = PdfReader(path)
    writer = PdfWriter()
    writer.append(reader, import_outline=False)
    titles = ["封面", "书名", "版权", "前言", "目录"] + [str(number) for number in range(1, 182)]
    for index, title in enumerate(titles):
        writer.add_outline_item(title, index % len(reader.pages))
    temporary = path.with_name("with-page-numbers.pdf")
    with temporary.open("wb") as stream:
        writer.write(stream)
    temporary.replace(path)


EXPECTED_TOC_OUTLINE = [
    ("Chapter 1 Introduction", 4, 1),
    ("1.1 Scope", 5, 2),
    ("Chapter 2 Methods", 7, 1),
    ("2.1 Setup", 9, 2),
    ("Appendix", 10, 1),
]
# Every written outline starts with a bookmark to the contents page itself.
EXPECTED_WRITTEN_OUTLINE = [("Contents", 2)] + [
    (title, page) for title, page, _ in EXPECTED_TOC_OUTLINE]


def add_structured_bookmarks(path: Path, entries: list[tuple[str, int, int]]) -> None:
    reader = PdfReader(path)
    writer = PdfWriter()
    writer.append(reader, import_outline=False)
    parents = {}
    for title, page, level in entries:
        parent = parents.get(level - 1) if level > 1 else None
        parents[level] = writer.add_outline_item(title, page - 1, parent=parent)
    temporary = path.with_name("with-structured-outline.pdf")
    with temporary.open("wb") as stream:
        writer.write(stream)
    temporary.replace(path)


class FakeRenderer:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def render(self, page_number: int, *, scale: int = 1700) -> Image.Image:
        self.calls.append((page_number, scale))
        return scanned_page(page_number)


class FakeVisionClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[int]]] = []

    def ask_json(self, prompt: str, images: list[tuple[int, Image.Image]], *,
                 max_tokens: int = 4096) -> dict:
        numbers = [number for number, image in images]
        assert all(image.width > 0 and image.height > 0 for _, image in images)
        if '"toc_pages"' in prompt:
            self.calls.append(("locate", numbers))
            return {"toc_pages": [number for number in numbers if number == 2]}
        if '"is_toc"' in prompt:
            self.calls.append(("toc", numbers))
            assert len(numbers) == 2 and numbers[0] == numbers[1]
            if numbers[0] != 2:
                return {"is_toc": False, "uncertain": False, "entries": []}
            return {"is_toc": True, "uncertain": False, "entries": [
                {"title": "Chapter 1 Introduction", "printed_page": "1", "level": 1},
                {"title": "1.1 Scope", "printed_page": "2", "level": 2},
                {"title": "Chapter 2 Methods", "printed_page": "4", "level": 1},
                {"title": "2.1 Setup", "printed_page": "6", "level": 2},
                {"title": "Appendix", "printed_page": "7", "level": 1},
            ]}
        if '"labels"' in prompt:
            self.calls.append(("labels", numbers))
            return {"labels": [{"pdf_page": number, "printed_page": str(number - 3)}
                               for number in numbers if number >= 4]}
        raise AssertionError(f"Unexpected prompt: {prompt}")


def flat_outline(pdf: PdfReader, items: list) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for item in items:
        if isinstance(item, list):
            found.extend(flat_outline(pdf, item))
        else:
            found.append((item["/Title"], pdf.get_destination_page_number(item) + 1))
    return found


class DeepSeekTest(unittest.TestCase):
    def test_default_renderer_processes_scanned_book_without_external_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scan.pdf"
            output = root / "bookmarked.pdf"
            make_scanned_book(source)

            with patch("subprocess.run", side_effect=AssertionError("external command launched")):
                result = process_book_deepseek(
                    source, output, root / "cache", api_key="unused", front=4, back=0,
                    client=FakeVisionClient())

            self.assertEqual(result.status, "success", result.as_dict())
            pdf = PdfReader(output)
            self.assertEqual(flat_outline(pdf, pdf.outline), EXPECTED_WRITTEN_OUTLINE)

    def test_pdfminer_open_failure_uses_visual_labels_after_toc_recognition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scan.pdf"
            output = root / "bookmarked.pdf"
            make_scanned_book(source)
            client = FakeVisionClient()

            with patch("bookmarker.deepseek.pdfplumber.open",
                       side_effect=PdfminerException(
                           PDFSyntaxError("No /Root object! - Is this really a PDF?"))):
                result = process_book_deepseek(
                    source, output, root / "cache", api_key="unused",
                    front=4, back=0, client=client, renderer=FakeRenderer())

            self.assertEqual(result.status, "success", result.as_dict())
            self.assertEqual(result.offsets, {"arabic": 3})
            self.assertGreaterEqual(len(result.anchors), 3)
            self.assertTrue(any(kind == "labels" for kind, _ in client.calls))
            pdf = PdfReader(output)
            self.assertEqual(flat_outline(pdf, pdf.outline), EXPECTED_WRITTEN_OUTLINE)

    def test_pdfminer_page_text_failure_uses_visual_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scan.pdf"
            output = root / "bookmarked.pdf"
            make_scanned_book(source)
            client = FakeVisionClient()

            with patch("bookmarker.deepseek._pdfplumber_words",
                       side_effect=PDFSyntaxError("broken native text")):
                result = process_book_deepseek(
                    source, output, root / "cache", api_key="unused",
                    front=4, back=0, client=client, renderer=FakeRenderer())

            self.assertEqual(result.status, "success", result.as_dict())
            self.assertEqual(result.offsets, {"arabic": 3})
            self.assertTrue(any(kind == "labels" for kind, _ in client.calls))

    def test_pdfminer_failure_without_visual_anchors_keeps_original(self) -> None:
        class NoLabelsClient(FakeVisionClient):
            def ask_json(self, prompt, images, *, max_tokens=4096):
                if '"labels"' in prompt:
                    return {"labels": []}
                return super().ask_json(prompt, images, max_tokens=max_tokens)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scan.pdf"
            make_scanned_book(source)
            original = source.read_bytes()

            with patch("bookmarker.deepseek.pdfplumber.open",
                       side_effect=PdfminerException(PDFSyntaxError("No /Root object"))):
                result = process_book_deepseek(
                    source, source, root / "cache", api_key="unused",
                    front=4, back=0, client=NoLabelsClient(), renderer=FakeRenderer())

            self.assertEqual(result.status, "needs_review", result.as_dict())
            self.assertEqual(len(result.entries), len(EXPECTED_TOC_OUTLINE))
            self.assertFalse(result.offsets)
            self.assertEqual(source.read_bytes(), original)

    def test_renderer_rejects_unbounded_output_size(self) -> None:
        with self.assertRaisesRegex(ValueError, "scale"):
            PageRenderer(Path("unused.pdf")).render(1, scale=10000)

    def test_cached_labels_reused_across_different_batch_groupings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scan.pdf"
            cache = root / "cache"
            cache.mkdir()
            make_scanned_book(source)
            path = cache / f"{PROMPT_VERSION}-{DEEPSEEK_MODEL}-labels-4-5-6-7.json"
            path.write_text(json.dumps({"labels": [
                {"pdf_page": page, "printed_page": str(page - 3)}
                for page in (4, 5, 6, 7)]}), encoding="utf-8")
            client = FakeVisionClient()
            renderer = FakeRenderer()
            labels = VisionPageLabels(source, 10, renderer, client, cache)
            with pdfplumber.open(source) as pdf:
                labels.prefetch(pdf, [4, 6])
            self.assertEqual(labels.labels[4], [("arabic", 1)])
            self.assertEqual(labels.labels[6], [("arabic", 3)])
            self.assertEqual(client.calls, [])
            self.assertEqual(renderer.calls, [])

    def test_page_number_outline_is_replaced_after_successful_recognition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scan.pdf"
            output = root / "bookmarked.pdf"
            make_scanned_book(source)
            add_page_number_bookmarks(source)
            result = process_book_deepseek(source, output, root / "cache", api_key="unused",
                                           front=4, back=0, client=FakeVisionClient(),
                                           renderer=FakeRenderer())
            self.assertEqual(result.status, "success", result.as_dict())
            self.assertEqual(result.existing_bookmarks, 186)
            self.assertLess(result.existing_outline_quality, 0.55)
            pdf = PdfReader(output)
            titles = [title for title, _ in flat_outline(pdf, pdf.outline)]
            self.assertEqual(titles, ["Contents", "Chapter 1 Introduction", "1.1 Scope",
                                      "Chapter 2 Methods", "2.1 Setup", "Appendix"])

    def test_skip_bookmarked_avoids_all_model_and_render_calls_even_when_replacing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scan.pdf"
            make_scanned_book(source)
            add_structured_bookmarks(source, [("Incorrect old bookmark", 4, 1)])
            original = source.read_bytes()
            client = FakeVisionClient()
            renderer = FakeRenderer()

            result = process_book_deepseek(
                source, source, root / "cache", api_key="unused", front=4, back=0,
                client=client, renderer=renderer, skip_existing=False,
                verify_existing=True, dry_run=True, skip_bookmarked=True)

            self.assertEqual(result.status, "skipped", result.as_dict())
            self.assertEqual(result.existing_bookmarks, 1)
            self.assertEqual(result.toc_pages, [])
            self.assertEqual(client.calls, [])
            self.assertEqual(renderer.calls, [])
            self.assertEqual(source.read_bytes(), original)

    def test_skip_bookmarked_processes_pdf_without_bookmarks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scan.pdf"
            output = root / "bookmarked.pdf"
            make_scanned_book(source)
            client = FakeVisionClient()
            result = process_book_deepseek(
                source, output, root / "cache", api_key="unused", front=4, back=0,
                client=client, renderer=FakeRenderer(), skip_bookmarked=True)
            self.assertEqual(result.status, "success", result.as_dict())
            self.assertTrue(client.calls)
            self.assertTrue(output.is_file())

    def test_verify_existing_skips_outline_matching_printed_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scan.pdf"
            output = root / "bookmarked.pdf"
            make_scanned_book(source)
            add_structured_bookmarks(source, EXPECTED_TOC_OUTLINE)
            client = FakeVisionClient()

            result = process_book_deepseek(source, output, root / "cache", api_key="unused",
                                           front=4, back=0, client=client,
                                           renderer=FakeRenderer(), verify_existing=True)

            self.assertEqual(result.status, "skipped", result.as_dict())
            self.assertEqual(result.toc_pages, [2])
            self.assertTrue(any(kind == "toc" for kind, _ in client.calls))
            self.assertFalse(output.exists())

    def test_written_outline_starts_with_contents_page_and_rechecks_as_matching(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scan.pdf"
            output = root / "bookmarked.pdf"
            make_scanned_book(source)

            first = process_book_deepseek(source, output, root / "cache", api_key="unused",
                                          front=4, back=0, client=FakeVisionClient(),
                                          renderer=FakeRenderer())
            self.assertEqual(first.status, "success", first.as_dict())
            self.assertEqual((first.toc_bookmark["title"], first.toc_bookmark["level"],
                              first.toc_bookmark["pdf_page"]), ("Contents", 1, 2))
            # The report keeps listing only the recognized entries.
            self.assertNotIn("Contents", [entry["title"] for entry in first.entries])
            written = output.read_bytes()

            # Checking the tool's own output again finds nothing to change.
            again = process_book_deepseek(output, root / "again.pdf", root / "cache-again",
                                          api_key="unused", front=4, back=0,
                                          client=FakeVisionClient(), renderer=FakeRenderer(),
                                          verify_existing=True)
            self.assertEqual(again.status, "skipped", again.as_dict())
            self.assertIsNone(again.toc_bookmark)
            self.assertFalse((root / "again.pdf").exists())
            self.assertEqual(output.read_bytes(), written)

    def test_only_a_contents_bookmark_on_the_contents_page_is_ignored(self) -> None:
        cases = {"on the contents page": (2, "skipped"), "elsewhere": (5, "success")}
        for name, (page, status) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "scan.pdf"
                output = root / "bookmarked.pdf"
                make_scanned_book(source)
                add_structured_bookmarks(source, [("Contents", page, 1), *EXPECTED_TOC_OUTLINE])

                result = process_book_deepseek(
                    source, output, root / "cache", api_key="unused", front=4, back=0,
                    client=FakeVisionClient(), renderer=FakeRenderer(), verify_existing=True)

                self.assertEqual(result.status, status, result.as_dict())
                if status == "success":
                    self.assertEqual(result.existing_outline_check["unexpected_titles"], 1)
                    pdf = PdfReader(output)
                    self.assertEqual(flat_outline(pdf, pdf.outline), EXPECTED_WRITTEN_OUTLINE)

    def test_verify_existing_replaces_missing_or_incorrect_outline(self) -> None:
        cases = {
            "missing section": EXPECTED_TOC_OUTLINE[:3] + EXPECTED_TOC_OUTLINE[4:],
            "incorrect title": EXPECTED_TOC_OUTLINE[:2]
                               + [("Chapter 2 Outdated", 7, 1)]
                               + EXPECTED_TOC_OUTLINE[3:],
            "incorrect page": EXPECTED_TOC_OUTLINE[:2]
                              + [("Chapter 2 Methods", 8, 1)]
                              + EXPECTED_TOC_OUTLINE[3:],
        }
        for name, outline in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "scan.pdf"
                output = root / "bookmarked.pdf"
                make_scanned_book(source)
                add_structured_bookmarks(source, outline)

                result = process_book_deepseek(
                    source, output, root / "cache", api_key="unused", front=4, back=0,
                    client=FakeVisionClient(), renderer=FakeRenderer(), verify_existing=True)

                self.assertEqual(result.status, "success", result.as_dict())
                pdf = PdfReader(output)
                self.assertEqual(flat_outline(pdf, pdf.outline), EXPECTED_WRITTEN_OUTLINE)
                self.assertEqual(len(pdf.outline), 6)

    def test_verify_existing_does_not_write_when_contents_are_untrusted(self) -> None:
        class NoContentsClient(FakeVisionClient):
            def ask_json(self, prompt, images, *, max_tokens=4096):
                if '"toc_pages"' in prompt:
                    return {"toc_pages": []}
                return super().ask_json(prompt, images, max_tokens=max_tokens)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scan.pdf"
            output = root / "bookmarked.pdf"
            make_scanned_book(source)
            add_structured_bookmarks(source, EXPECTED_TOC_OUTLINE)
            original = source.read_bytes()

            result = process_book_deepseek(
                source, output, root / "cache", api_key="unused", front=4, back=0,
                client=NoContentsClient(), renderer=FakeRenderer(), verify_existing=True)

            self.assertEqual(result.status, "needs_review", result.as_dict())
            self.assertFalse(output.exists())
            self.assertEqual(source.read_bytes(), original)

    def test_in_place_replace_holds_unrelated_useful_outline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scan.pdf"
            make_scanned_book(source)
            unrelated = [(f"Chapter {index} Unrelated", page, 1)
                         for index, page in enumerate((4, 5, 7, 9, 10), 1)]
            add_structured_bookmarks(source, unrelated)
            original = source.read_bytes()

            result = process_book_deepseek(
                source, source, root / "cache", api_key="unused", front=4, back=0,
                client=FakeVisionClient(), renderer=FakeRenderer(), verify_existing=True)

            self.assertEqual(result.status, "needs_review", result.as_dict())
            self.assertEqual(result.existing_outline_check["matched_titles"], 0)
            self.assertEqual(source.read_bytes(), original)

    def test_client_sends_key_only_in_authorization_header(self) -> None:
        secret = "test-secret-not-for-request-body"
        requests = []

        def open_response(request, *, timeout):
            requests.append(request)
            self.assertEqual(timeout, 120)
            response = {"choices": [{"finish_reason": "stop",
                                     "message": {"content": '{"toc_pages":[2]}'}}]}
            return io.BytesIO(json.dumps(response).encode("utf-8"))

        client = DeepSeekClient(secret, opener=open_response)
        answer = client.ask_json("Find the contents page", [(2, scanned_page(2))])

        self.assertEqual(answer, {"toc_pages": [2]})
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.get_header("Authorization"), f"Bearer {secret}")
        self.assertNotIn(secret, request.full_url)
        self.assertNotIn(secret, request.data.decode("utf-8"))
        payload = json.loads(request.data)
        self.assertTrue(payload["model"])
        self.assertTrue(any(item["type"] == "image_url"
                            for item in payload["messages"][0]["content"]))

    def test_client_accumulates_only_response_reported_token_usage(self) -> None:
        responses = iter([
            {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
             "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}},
            {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}],
             "usage": {"prompt_tokens": 30, "completion_tokens": 8, "total_tokens": 38}},
            {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]},
            {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
             "usage": {"prompt_tokens": True, "completion_tokens": 2, "total_tokens": 3}},
        ])
        client = DeepSeekClient("test-key", opener=lambda _request, *, timeout:
                                io.BytesIO(json.dumps(next(responses)).encode("utf-8")))
        self.assertEqual(client.ask_json("first", []), {})
        with self.assertRaises(DeepSeekError):
            client.ask_json("truncated", [])
        self.assertEqual(client.ask_json("missing usage", []), {})
        self.assertEqual(client.ask_json("invalid usage", []), {})
        self.assertEqual(client.api_usage, {
            "prompt_tokens": 130, "completion_tokens": 28, "total_tokens": 158,
            "reported_responses": 2, "unreported_responses": 2,
        })

    def test_book_result_counts_only_this_processing_run_even_with_shared_client(self) -> None:
        class TrackedClient(FakeVisionClient):
            def __init__(self) -> None:
                super().__init__()
                self.api_usage = {"prompt_tokens": 0, "completion_tokens": 0,
                                  "total_tokens": 0, "reported_responses": 0,
                                  "unreported_responses": 0}

            def ask_json(self, prompt, images, *, max_tokens=4096):
                answer = super().ask_json(prompt, images, max_tokens=max_tokens)
                self.api_usage["prompt_tokens"] += 10
                self.api_usage["completion_tokens"] += 2
                self.api_usage["total_tokens"] += 12
                self.api_usage["reported_responses"] += 1
                return answer

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scan.pdf"
            make_scanned_book(source)
            client = TrackedClient()
            first = process_book_deepseek(source, root / "first.pdf", root / "cache",
                                          api_key="unused", front=4, back=0,
                                          client=client, renderer=FakeRenderer())
            self.assertEqual(first.status, "success", first.as_dict())
            self.assertGreater(first.api_usage["total_tokens"], 0)
            calls_after_first = len(client.calls)
            second = process_book_deepseek(source, root / "second.pdf", root / "cache",
                                           api_key="unused", front=4, back=0,
                                           client=client, renderer=FakeRenderer())
            self.assertEqual(second.status, "success", second.as_dict())
            self.assertEqual(len(client.calls), calls_after_first)
            self.assertEqual(second.api_usage["total_tokens"], 0)
            self.assertEqual(second.api_usage["reported_responses"], 0)

    def test_default_opener_rejects_same_origin_and_cross_origin_redirects(self) -> None:
        client = DeepSeekClient("test-secret")
        opener = client._opener.__self__
        handlers = [handler for handler in opener.handlers
                    if isinstance(handler, urllib.request.HTTPRedirectHandler)]
        self.assertTrue(handlers)
        request = urllib.request.Request(API_URL, data=b"{}", method="POST",
                                         headers={"Authorization": "Bearer test-secret"})
        for handler in handlers:
            for code in (301, 302, 303, 307, 308):
                for destination in (API_URL, "https://other.example/receive"):
                    with self.subTest(code=code, destination=destination):
                        self.assertIsNone(handler.redirect_request(
                            request, None, code, "redirect", {"Location": destination},
                            destination))

    def test_client_rejects_unusable_model_json(self) -> None:
        responses = [
            {"choices": [{"finish_reason": "stop", "message": {"content": "not JSON"}}]},
            {"choices": [{"finish_reason": "stop", "message": {"content": "[]"}}]},
            {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
            {"choices": []},
        ]
        for response in responses:
            with self.subTest(response=response):
                client = DeepSeekClient(
                    "do-not-expose-this-key",
                    opener=lambda _request, *, timeout: io.BytesIO(
                        json.dumps(response).encode("utf-8")),
                )
                with self.assertRaises(DeepSeekError) as caught:
                    client.ask_json("Return JSON", [])
                self.assertNotIn("do-not-expose-this-key", str(caught.exception))

    def test_toc_entries_reject_invalid_model_fields(self) -> None:
        valid = {"is_toc": True, "uncertain": False, "entries": [
            {"title": "Chapter 1", "printed_page": "1", "level": 1}]}
        invalid = [
            {"is_toc": "true", "uncertain": False, "entries": []},
            {"is_toc": True, "uncertain": False, "entries": {}},
            {"is_toc": True, "uncertain": "false", "entries": []},
            {"is_toc": True, "uncertain": False, "entries": [
                {"title": "", "printed_page": "1", "level": 1}]},
            {"is_toc": True, "uncertain": False, "entries": [
                {"title": "Chapter 1", "printed_page": True, "level": 1}]},
            {"is_toc": True, "uncertain": False, "entries": [
                {"title": "Chapter 1", "printed_page": "1", "level": 7}]},
        ]
        for response in invalid:
            with self.subTest(response=response), self.assertRaises(DeepSeekError):
                _parse_toc_entries(response, pdf_page=2, page_count=10)
        entries, warnings = _parse_toc_entries(valid, pdf_page=2, page_count=10)
        self.assertEqual((entries[0].title, entries[0].printed_page), ("Chapter 1", 1))
        self.assertEqual(warnings, [])

    def test_toc_parser_preserves_visual_levels_for_unnumbered_peers(self) -> None:
        raw = {"is_toc": True, "uncertain": False, "entries": [
            {"title": "Chapter 5. The Eye", "printed_page": "120", "level": 1},
            {"title": "5.1 Introduction", "printed_page": "121", "level": 2},
            {"title": "Bibliography", "printed_page": "125", "level": 2},
            {"title": "Exercises", "printed_page": "126", "level": 2},
            {"title": "Chapter 6. Stops and Apertures", "printed_page": "128", "level": 1},
        ]}
        entries, warnings = _parse_toc_entries(raw, pdf_page=7, page_count=300)
        self.assertEqual(warnings, [])
        self.assertEqual([(entry.title, entry.level) for entry in entries], [
            ("Chapter 5. The Eye", 1), ("5.1 Introduction", 2),
            ("Bibliography", 2), ("Exercises", 2),
            ("Chapter 6. Stops and Apertures", 1),
        ])

    def test_toc_prompt_uses_visual_level_and_refreshes_only_toc_cache(self) -> None:
        class RecordingClient:
            def __init__(self) -> None:
                self.prompts: list[str] = []

            def ask_json(self, prompt, images, *, max_tokens=4096):
                self.prompts.append(prompt)
                return {"is_toc": True, "uncertain": False, "entries": [
                    {"title": "Chapter 5. The Eye", "printed_page": "120", "level": 1},
                    {"title": "5.1 Introduction", "printed_page": "121", "level": 2},
                    {"title": "Bibliography", "printed_page": "125", "level": 2},
                ]}

        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            stale = cache / f"{PROMPT_VERSION}-{DEEPSEEK_MODEL}-toc-7.json"
            stale.write_text(json.dumps({"is_toc": True, "uncertain": False,
                                         "entries": [{"title": "Bibliography",
                                                      "printed_page": "125", "level": 1}]}),
                             encoding="utf-8")
            client = RecordingClient()
            entries, _, _ = _extract_toc(7, 300, FakeRenderer(), client, cache)
            self.assertEqual([entry.level for entry in entries], [1, 2, 2])
            self.assertEqual(len(client.prompts), 1)
            self.assertIn("同一视觉层级的条目给相同 level", client.prompts[0])
            self.assertIn("Bibliography、Exercises 与 5.1、5.2 同缩进", client.prompts[0])
            self.assertTrue((cache / f"{PROMPT_VERSION}-{DEEPSEEK_MODEL}-toc-"
                                   f"{TOC_PROMPT_VERSION}-7.json").is_file())
            _extract_toc(7, 300, FakeRenderer(), client, cache)
            self.assertEqual(len(client.prompts), 1)

    def test_unnumbered_chapter_is_omitted_even_with_a_numbered_child(self) -> None:
        raw = {"is_toc": True, "uncertain": False, "entries": [
            {"title": "Chapter 1 Introduction", "printed_page": "", "level": 1},
            {"title": "1.1 Scope", "printed_page": "3", "level": 2},
            {"title": "Chapter 2 Methods", "printed_page": "8", "level": 1},
        ]}
        entries, warnings = _parse_toc_entries(raw, pdf_page=2, page_count=10)
        self.assertEqual(warnings, [])
        self.assertEqual([(entry.title, entry.printed_page, entry.numbering)
                          for entry in entries], [
            ("1.1 Scope", 3, "arabic"),
            ("Chapter 2 Methods", 8, "arabic"),
        ])
        self.assertNotIn("Chapter 1 Introduction", [entry.title for entry in entries])

        without_child = {"is_toc": True, "uncertain": False, "entries": [
            {"title": "Chapter 1 Introduction", "printed_page": "", "level": 1},
            {"title": "Chapter 2 Methods", "printed_page": "8", "level": 1},
        ]}
        entries, warnings = _parse_toc_entries(without_child, pdf_page=2, page_count=10)
        self.assertEqual(warnings, [])
        self.assertEqual([entry.title for entry in entries], ["Chapter 2 Methods"])

        visually_different_levels = {"is_toc": True, "uncertain": False, "entries": [
            {"title": "第2章 特殊矩阵", "printed_page": "", "level": 2},
            {"title": "2.1 Hermitian 矩阵", "printed_page": "101", "level": 1},
        ]}
        parsed, warnings = _parse_toc_entries(visually_different_levels, pdf_page=2,
                                              page_count=120)
        self.assertEqual(warnings, [])
        self.assertEqual([(entry.printed_page, entry.level) for entry in parsed],
                         [(101, 1)])

    def test_unpaged_frontmatter_is_omitted_without_losing_numbered_contents(self) -> None:
        raw = {"is_toc": True, "uncertain": False, "entries": [
            {"title": "译者序", "printed_page": "", "level": 1},
            {"title": "前言", "printed_page": None, "level": 1},
            {"title": "给学生的注释", "printed_page": "—", "level": 1},
            {"title": "关于作者", "printed_page": "?", "level": 1},
            {"title": "第1章 线性方程组", "printed_page": "1", "level": 1},
            {"title": "1.1 线性方程组", "printed_page": "2", "level": 2},
        ]}
        entries, warnings = _parse_toc_entries(raw, pdf_page=15, page_count=642)
        self.assertEqual(warnings, [])
        self.assertEqual([(entry.title, entry.printed_page) for entry in entries], [
            ("第1章 线性方程组", 1), ("1.1 线性方程组", 2),
        ])

    def test_unpaged_numbered_section_is_omitted(self) -> None:
        raw = {"is_toc": True, "uncertain": False, "entries": [
            {"title": "第1章 线性方程组", "printed_page": "1", "level": 1},
            {"title": "1.1 线性方程组", "printed_page": "", "level": 2},
        ]}
        entries, warnings = _parse_toc_entries(raw, pdf_page=15, page_count=642)
        self.assertEqual(warnings, [])
        self.assertEqual([entry.title for entry in entries], ["第1章 线性方程组"])

        raw["entries"][1]["title"] = "没有编号但印刷页码看不清"
        raw["entries"][1]["printed_page"] = "?"
        entries, warnings = _parse_toc_entries(raw, pdf_page=15, page_count=642)
        self.assertEqual(warnings, [])
        self.assertEqual([entry.title for entry in entries], ["第1章 线性方程组"])

    def test_only_numeric_printed_page_labels_become_entries(self) -> None:
        raw = {"is_toc": True, "uncertain": False, "entries": [
            {"title": "Foreword", "printed_page": "iv", "level": 1},
            {"title": "Chapter 1", "printed_page": "1", "level": 1},
            {"title": "1.1 Unreadable", "printed_page": "?", "level": 2},
            {"title": "1.2 Mixed suffix", "printed_page": "12a", "level": 2},
            {"title": "1.3 Clear", "printed_page": 12, "level": 2},
            {"title": "1.4 Parenthesized Roman", "printed_page": "(iv)", "level": 2},
            {"title": "1.5 Parenthesized mixed", "printed_page": "(12a)", "level": 2},
            {"title": "Appendix", "printed_page": "—", "level": 1},
        ]}
        entries, warnings = _parse_toc_entries(raw, pdf_page=2, page_count=20)
        self.assertEqual(warnings, [])
        self.assertEqual([(entry.title, entry.printed_page) for entry in entries],
                         [("Chapter 1", 1), ("1.3 Clear", 12)])

    def test_parenthesized_arabic_printed_pages_become_entries(self) -> None:
        for label, expected in (("(1)", 1), (" ( 12 ) ", 12), ("（１）", 1),
                                ("（ １２ ）", 12)):
            with self.subTest(label=label):
                raw = {"is_toc": True, "uncertain": False, "entries": [
                    {"title": "§0.1 近世代数的创立", "printed_page": label, "level": 2},
                ]}
                entries, warnings = _parse_toc_entries(raw, pdf_page=11,
                                                       page_count=200)
                self.assertEqual(warnings, [])
                self.assertEqual([(entry.title, entry.printed_page,
                                   entry.numbering) for entry in entries],
                                 [("§0.1 近世代数的创立", expected, "arabic")])

    def test_parenthesized_arabic_footer_label_matches_toc_label(self) -> None:
        labels = VisionPageLabels(Path("unused.pdf"), 10, FakeRenderer(),
                                  FakeVisionClient(), Path("unused-cache"))
        parsed = labels._parse_labels({"labels": [
            {"pdf_page": 4, "printed_page": "（１）"},
        ]}, [4])
        self.assertEqual(parsed, {4: [("arabic", 1)]})

    def test_numeric_zero_and_out_of_range_pages_still_fail(self) -> None:
        for printed_page in ("0", "221", "(0)", "(221)", "（０）"):
            with self.subTest(printed_page=printed_page):
                raw = {"is_toc": True, "uncertain": False, "entries": [
                    {"title": "Chapter 1", "printed_page": printed_page, "level": 1},
                ]}
                with self.assertRaisesRegex(DeepSeekError, "目录页码无效"):
                    _parse_toc_entries(raw, pdf_page=2, page_count=20)

    def test_invalid_location_response_is_not_cached(self) -> None:
        class SequenceClient:
            def __init__(self) -> None:
                self.responses = [{"toc_pages": "2"}, {"toc_pages": [2]}]
                self.calls = 0

            def ask_json(self, prompt, images, *, max_tokens=4096):
                self.calls += 1
                return self.responses.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache"
            client = SequenceClient()
            renderer = FakeRenderer()
            with self.assertRaises(DeepSeekError):
                _discover_toc(4, 4, 0, renderer, client, cache)
            self.assertEqual(list(cache.glob("*.json")), [])
            self.assertEqual(_discover_toc(4, 4, 0, renderer, client, cache), [2])
            self.assertEqual(client.calls, 2)
            self.assertEqual(len(list(cache.glob("*.json"))), 1)

    def test_front_scan_continues_after_first_empty_batch(self) -> None:
        class LateContentsClient:
            def __init__(self) -> None:
                self.batches: list[list[int]] = []

            def ask_json(self, prompt, images, *, max_tokens=4096):
                numbers = [number for number, _ in images]
                self.batches.append(numbers)
                return {"toc_pages": [9] if 9 in numbers else []}

        with tempfile.TemporaryDirectory() as directory:
            client = LateContentsClient()
            pages = _discover_toc(18, 18, 0, FakeRenderer(), client,
                                  Path(directory) / "cache")
            self.assertEqual(pages, [9])
            self.assertIn(list(range(1, 7)), client.batches)
            self.assertIn(list(range(7, 13)), client.batches)

    def test_scanned_pdf_gets_correct_bookmarks_and_reuses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scan.pdf"
            make_scanned_book(source)
            self.assertTrue(all(not (page.extract_text() or "").strip()
                                for page in PdfReader(source).pages))
            client = FakeVisionClient()
            renderer = FakeRenderer()
            cache = root / "cache"

            first = process_book_deepseek(source, root / "first.pdf", cache, api_key="unused",
                                          front=4, back=0, client=client, renderer=renderer)
            self.assertEqual(first.status, "success", first.as_dict())
            self.assertEqual(first.toc_pages, [2])
            self.assertEqual(first.offsets, {"arabic": 3})
            self.assertTrue(any(kind == "labels" for kind, _ in client.calls))
            self.assertTrue(renderer.calls)
            expected = EXPECTED_WRITTEN_OUTLINE
            output = PdfReader(root / "first.pdf")
            self.assertEqual(flat_outline(output, output.outline), expected)

            calls_before = list(client.calls)
            cached_renderer = FakeRenderer()
            second = process_book_deepseek(source, root / "second.pdf", cache, api_key="unused",
                                           front=4, back=0, client=client,
                                           renderer=cached_renderer)
            self.assertEqual(second.status, "success", second.as_dict())
            self.assertEqual(client.calls, calls_before)
            self.assertEqual(cached_renderer.calls, [])
            cached_output = PdfReader(root / "second.pdf")
            self.assertEqual(flat_outline(cached_output, cached_output.outline), expected)

    def test_roman_frontmatter_is_omitted_and_arabic_bookmarks_exported(self) -> None:
        class RomanFrontMatterClient(FakeVisionClient):
            def ask_json(self, prompt, images, *, max_tokens=4096):
                answer = super().ask_json(prompt, images, max_tokens=max_tokens)
                if '"is_toc"' in prompt and images[0][0] == 2:
                    answer["entries"].insert(0, {"title": "Preface", "printed_page": "ix",
                                                 "level": 1})
                return answer

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scan.pdf"
            output = root / "bookmarked.pdf"
            make_scanned_book(source)
            result = process_book_deepseek(source, output, root / "cache", api_key="unused",
                                           front=4, back=0, client=RomanFrontMatterClient(),
                                           renderer=FakeRenderer())

            self.assertEqual(result.status, "success", result.as_dict())
            self.assertEqual(result.offsets, {"arabic": 3})
            self.assertTrue(all(entry["numbering"] == "arabic" for entry in result.entries))
            self.assertNotIn("Preface", [entry["title"] for entry in result.entries])
            pdf = PdfReader(output)
            self.assertEqual(flat_outline(pdf, pdf.outline), EXPECTED_WRITTEN_OUTLINE)

    def test_all_roman_toc_page_does_not_block_following_numeric_toc_page(self) -> None:
        class TwoPageContentsClient(FakeVisionClient):
            def ask_json(self, prompt, images, *, max_tokens=4096):
                if '"toc_pages"' in prompt:
                    return {"toc_pages": [page for page, _ in images if page in (2, 3)]}
                if '"is_toc"' in prompt:
                    page = images[0][0]
                    if page == 2:
                        return {"is_toc": True, "uncertain": False, "entries": [
                            {"title": "Foreword", "printed_page": "i", "level": 1},
                            {"title": "Preface", "printed_page": "iii", "level": 1},
                            {"title": "Acknowledgments", "printed_page": "v", "level": 1},
                        ]}
                    if page == 3:
                        answer = super().ask_json(prompt, [(2, image) for _, image in images],
                                                  max_tokens=max_tokens)
                        return answer
                if '"labels"' in prompt:
                    return {"labels": [
                        {"pdf_page": page, "printed_page":
                         "ii" if page == 4 else "iii" if page == 5 else str(page - 5)}
                        for page, _ in images if page >= 4]}
                return super().ask_json(prompt, images, max_tokens=max_tokens)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scan.pdf"
            output = root / "bookmarked.pdf"
            writer = PdfWriter()
            for _ in range(12):
                writer.add_blank_page(width=612, height=792)
            writer.write(source)

            result = process_book_deepseek(source, output, root / "cache", api_key="unused",
                                           front=4, back=0, client=TwoPageContentsClient(),
                                           renderer=FakeRenderer())

            self.assertEqual(result.status, "success", result.as_dict())
            self.assertEqual(len(result.entries), len(EXPECTED_TOC_OUTLINE))
            self.assertEqual(result.offsets, {"arabic": 5})
            self.assertTrue(all(entry["numbering"] == "arabic" for entry in result.entries))
            pdf = PdfReader(output)
            self.assertEqual(flat_outline(pdf, pdf.outline), [("Contents", 2)] + [
                (title, page + 2) for title, page, _ in EXPECTED_TOC_OUTLINE])

    def test_roman_bookmarks_are_omitted_even_when_roman_offset_can_be_fitted(self) -> None:
        class MappableRomanClient(FakeVisionClient):
            def ask_json(self, prompt, images, *, max_tokens=4096):
                if '"is_toc"' in prompt and images[0][0] == 2:
                    answer = super().ask_json(prompt, images, max_tokens=max_tokens)
                    answer["entries"][:0] = [
                        {"title": "Foreword", "printed_page": "i", "level": 1},
                        {"title": "Preface", "printed_page": "ii", "level": 1},
                        {"title": "Acknowledgments", "printed_page": "iii", "level": 1},
                    ]
                    return answer
                if '"labels"' in prompt:
                    return {"labels": [
                        {"pdf_page": page, "printed_page":
                         {3: "i", 4: "ii", 5: "iii"}.get(page, str(page - 5))}
                        for page, _ in images if page >= 3]}
                return super().ask_json(prompt, images, max_tokens=max_tokens)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scan.pdf"
            output = root / "bookmarked.pdf"
            writer = PdfWriter()
            for _ in range(12):
                writer.add_blank_page(width=612, height=792)
            writer.write(source)

            result = process_book_deepseek(source, output, root / "cache", api_key="unused",
                                           front=4, back=0, client=MappableRomanClient(),
                                           renderer=FakeRenderer())

            self.assertEqual(result.status, "success", result.as_dict())
            self.assertEqual(result.offsets, {"arabic": 5})
            self.assertEqual(len(result.entries), len(EXPECTED_TOC_OUTLINE))
            pdf = PdfReader(output)
            self.assertEqual(flat_outline(pdf, pdf.outline), [("Contents", 2)] + [
                (title, page + 2) for title, page, _ in EXPECTED_TOC_OUTLINE])

    def test_toc_without_any_numeric_page_does_not_write_empty_outline(self) -> None:
        class RomanOnlyClient(FakeVisionClient):
            def ask_json(self, prompt, images, *, max_tokens=4096):
                if '"is_toc"' in prompt and images[0][0] == 2:
                    return {"is_toc": True, "uncertain": False, "entries": [
                        {"title": "Preface", "printed_page": "iii", "level": 1},
                        {"title": "Acknowledgments", "printed_page": "v", "level": 1},
                    ]}
                return super().ask_json(prompt, images, max_tokens=max_tokens)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scan.pdf"
            output = root / "bookmarked.pdf"
            make_scanned_book(source)
            result = process_book_deepseek(source, output, root / "cache", api_key="unused",
                                           front=4, back=0, client=RomanOnlyClient(),
                                           renderer=FakeRenderer())

            self.assertEqual(result.status, "needs_review", result.as_dict())
            self.assertEqual(result.entries, [])
            self.assertFalse(output.exists())

    def test_single_arabic_page_regression_requires_review(self) -> None:
        class RegressingContentsClient(FakeVisionClient):
            def ask_json(self, prompt, images, *, max_tokens=4096):
                answer = super().ask_json(prompt, images, max_tokens=max_tokens)
                if '"is_toc"' in prompt and images[0][0] == 2:
                    answer["entries"][2]["printed_page"] = "1"
                return answer

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scan.pdf"
            output = root / "bookmarked.pdf"
            make_scanned_book(source)
            result = process_book_deepseek(source, output, root / "cache", api_key="unused",
                                           front=4, back=0, client=RegressingContentsClient(),
                                           renderer=FakeRenderer())
            self.assertEqual(result.status, "needs_review", result.as_dict())
            self.assertTrue(any("倒退" in warning for warning in result.warnings))
            self.assertFalse(output.exists())

    def test_unsubmitted_toc_page_fails_without_writing_pdf(self) -> None:
        class InvalidClient(FakeVisionClient):
            def ask_json(self, prompt, images, *, max_tokens=4096):
                return {"toc_pages": [99]}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scan.pdf"
            output = root / "output.pdf"
            make_scanned_book(source)
            result = process_book_deepseek(source, output, root / "cache", api_key="unused",
                                           front=4, back=0, client=InvalidClient(),
                                           renderer=FakeRenderer())
            self.assertEqual(result.status, "failed", result.as_dict())
            self.assertIn("未提交的页码", result.error)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
