"""Coordinate-aware text extraction from searchable and scanned PDFs."""

from __future__ import annotations

import json
import re
import statistics
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .resources import find_executable, ocr_script_path


@dataclass(frozen=True)
class Word:
    text: str
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class Line:
    text: str
    words: tuple[Word, ...]
    x: float
    y: float
    right: float
    bottom: float


@dataclass(frozen=True)
class PageContent:
    page_number: int
    width: float
    height: float
    lines: tuple[Line, ...]
    method: str


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("⋯", "…").replace("·", ".").replace("•", ".")
    text = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text)
    text = re.sub(r"\s+([，。；：、.])", r"\1", text)
    text = re.sub(r"([.。])\s+(?=[.。])", r"\1", text)
    return text.strip()


def _group_words(words: list[Word], width: float, height: float) -> tuple[Line, ...]:
    if not words:
        return ()

    # A distant right-hand page number does not count as column text.
    left_letters = sum(len(re.sub(r"[\W\d_]", "", w.text)) for w in words if w.x < width * 0.45)
    right_letters = sum(len(re.sub(r"[\W\d_]", "", w.text)) for w in words if w.x > width * 0.55)
    middle_letters = sum(len(re.sub(r"[\W\d_]", "", w.text)) for w in words if width * 0.45 <= w.x <= width * 0.55)
    two_columns = left_letters >= 18 and right_letters >= 18 and middle_letters < min(left_letters, right_letters) * 0.23

    columns: list[list[Word]]
    if two_columns:
        midpoint = width / 2
        columns = [[w for w in words if w.x < midpoint], [w for w in words if w.x >= midpoint]]
    else:
        columns = [words]

    output: list[Line] = []
    for column in columns:
        if not column:
            continue
        median_height = statistics.median(max(w.height, 1.0) for w in column)
        tolerance = max(3.0, median_height * 0.60)
        groups: list[list[Word]] = []
        for word in sorted(column, key=lambda w: (w.y + w.height * 0.5, w.x)):
            center = word.y + word.height * 0.5
            if groups:
                last_center = statistics.median(w.y + w.height * 0.5 for w in groups[-1])
                if abs(center - last_center) <= tolerance:
                    groups[-1].append(word)
                    continue
            groups.append([word])
        for group in groups:
            group.sort(key=lambda w: w.x)
            parts: list[str] = []
            previous: Word | None = None
            for word in group:
                if previous is not None:
                    gap = word.x - (previous.x + previous.width)
                    parts.append("   " if gap > median_height * 1.7 else " ")
                parts.append(word.text)
                previous = word
            output.append(Line(
                text=clean_text("".join(parts)),
                words=tuple(group),
                x=min(w.x for w in group),
                y=min(w.y for w in group),
                right=max(w.x + w.width for w in group),
                bottom=max(w.y + w.height for w in group),
            ))
    return tuple(output)


def _pdfplumber_words(page) -> list[Word]:
    result: list[Word] = []
    for item in page.extract_words(x_tolerance=2, y_tolerance=3, keep_blank_chars=False):
        text = item.get("text", "").strip()
        if text:
            result.append(Word(text, float(item["x0"]), float(item["top"]),
                               float(item["x1"] - item["x0"]), float(item["bottom"] - item["top"])))
    return result


def _usable_native_text(words: list[Word]) -> bool:
    """Reject empty text layers and common broken PDF character maps."""
    text = clean_text("".join(word.text for word in words))
    if len(text) < 35:
        return False
    broken = sum(character in "�□" or unicodedata.category(character) in {"Co", "Cn", "Cc"}
                 for character in text)
    readable = sum(character.isalpha() or character.isdigit() for character in text)
    return broken / len(text) < 0.08 and readable / len(text) >= 0.35


class Extractor:
    def __init__(self, pdf_path: Path, cache_dir: Path, ocr: str = "auto") -> None:
        self.pdf_path = pdf_path
        self.cache_dir = cache_dir
        self.ocr = ocr
        self._cache: dict[int, PageContent] = {}
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def page(self, pdf, page_number: int, *, force_ocr: bool = False) -> PageContent:
        if page_number in self._cache and not force_ocr:
            return self._cache[page_number]
        page = pdf.pages[page_number - 1]
        width, height = float(page.width), float(page.height)
        words = _pdfplumber_words(page)
        native_ok = _usable_native_text(words)
        if native_ok and not force_ocr:
            content = PageContent(page_number, width, height, _group_words(words, width, height), "text")
        elif self.ocr == "off":
            content = PageContent(page_number, width, height, _group_words(words, width, height), "text-weak")
        else:
            content = self._ocr_page(page_number, width, height)
            if not content.lines and words:
                content = PageContent(page_number, width, height, _group_words(words, width, height), "text-weak")
        self._cache[page_number] = content
        return content

    def _ocr_page(self, page_number: int, width: float, height: float) -> PageContent:
        path = self.cache_dir / f"page-{page_number}.json"
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
        else:
            with tempfile.TemporaryDirectory(prefix="pdf-bookmarker-") as temporary:
                image_path = Path(temporary) / "page.png"
                self._render(page_number, image_path, scale=1900)
                raw = self._run_ocr(image_path)
            path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        image_width, image_height = float(raw["width"]), float(raw["height"])
        words = [
            Word(str(w["text"]), float(w["x"]) * width / image_width,
                 float(w["y"]) * height / image_height,
                 float(w["width"]) * width / image_width,
                 float(w["height"]) * height / image_height)
            for line in raw.get("lines", []) for w in line.get("words", []) if str(w.get("text", "")).strip()
        ]
        return PageContent(page_number, width, height, _group_words(words, width, height), "ocr")

    def enhance_toc_numbers(self, pdf, page_number: int) -> PageContent:
        """Re-read OCR page-number rows that full-page recognition often omits."""
        content = self.page(pdf, page_number)
        if content.method != "ocr" or not content.lines:
            return content
        path = self.cache_dir / f"page-{page_number}-toc-numbers-v5.json"
        if path.exists():
            extra = [Word(**item) for item in json.loads(path.read_text(encoding="utf-8"))]
        else:
            extra: list[Word] = []
            with tempfile.TemporaryDirectory(prefix="pdf-bookmarker-toc-") as temporary:
                full = Path(temporary) / "page.png"
                self._render(page_number, full, scale=1900)
                with Image.open(full) as image:
                    image = image.convert("RGB")
                    # Latin OCR reads Arabic numerals more reliably than the Chinese
                    # model, even on an otherwise Chinese table of contents.
                    try:
                        raw = self._run_ocr_language(full, "en-US")
                    except (RuntimeError, json.JSONDecodeError):
                        return content
                    number_x = [float(word["x"]) for line in raw.get("lines", [])
                                for word in line.get("words", [])
                                if float(word["x"]) >= image.width * 0.75
                                and re.fullmatch(r"[0-9]{1,4}", str(word.get("text", "")))]
                    margin_x = int(round(statistics.median(number_x))) + 1 if number_x else int(image.width * 0.86)
                    margin_x = max(int(image.width * 0.72), min(margin_x, image.width - 25))

                    # Isolated one-digit labels are sometimes ignored altogether.
                    # Put each printed row beside a neutral text cue so OCR treats
                    # the tiny label as text; positions still map to the source row.
                    row_height = 100
                    cue_x, label_x = 20, 340
                    montage = Image.new("RGB", (620, len(content.lines) * row_height), "white")
                    draw = ImageDraw.Draw(montage)
                    try:
                        font = ImageFont.truetype("arial.ttf", 50)
                    except OSError:
                        font = ImageFont.load_default(size=50)
                    for index, line in enumerate(content.lines):
                        y = max(0, min(int(line.y * image.height / content.height), image.height - 1))
                        x1 = min(image.width, margin_x + max(55, round(image.width * 0.045)))
                        crop = image.crop((margin_x, max(0, y - 2), x1, min(image.height, y + 28)))
                        crop = crop.resize((max(1, crop.width * 2), 60), Image.Resampling.LANCZOS)
                        montage.paste(crop, (label_x, index * row_height + 20))
                        draw.text((cue_x, index * row_height + 20), "Page", fill="black", font=font)
                    location = Path(temporary) / "number-rows.png"
                    montage.save(location)
                    row_numbers: dict[int, str] = {}
                    # Full-page OCR finds some labels that the narrow strip
                    # misses and is usually more faithful for multi-digit
                    # labels. Match by vertical position at the established
                    # printed-number margin.
                    for line in raw.get("lines", []):
                        for word in line.get("words", []):
                            token = str(word.get("text", ""))
                            x = float(word["x"])
                            if (not re.fullmatch(r"[0-9]{1,4}", token)
                                    or x < image.width * 0.75
                                    or abs(x - margin_x) > image.width * 0.07):
                                continue
                            y = float(word["y"]) * content.height / image.height
                            index = min(range(len(content.lines)),
                                        key=lambda i: abs(content.lines[i].y - y))
                            if abs(content.lines[index].y - y) <= 8:
                                row_numbers[index] = token
                    try:
                        recognized = self._run_ocr_language(location, "en-US")
                    except (RuntimeError, json.JSONDecodeError):
                        recognized = {"lines": []}
                    for line in recognized.get("lines", []):
                        for word in line.get("words", []):
                            token = str(word.get("text", ""))
                            if float(word["x"]) < label_x or not re.fullmatch(r"[0-9]{1,4}", token):
                                continue
                            index = int(float(word["y"]) // row_height)
                            if index < len(content.lines):
                                row_numbers.setdefault(index, token)
                    for index, token in row_numbers.items():
                        row = content.lines[index]
                        row_height_pt = max((word.height for word in row.words), default=10.0)
                        extra.append(Word(token, margin_x * content.width / image.width,
                                          row.y, max(5.0, len(token) * row_height_pt * 0.5),
                                          max(5.0, row.bottom - row.y)))
            path.write_text(json.dumps([word.__dict__ for word in extra], ensure_ascii=False), encoding="utf-8")
        words = [word for line in content.lines for word in line.words]
        for word in extra:
            words = [old for old in words if not (
                old.x >= content.width * 0.75 and abs(old.y - word.y) < 8
                and re.fullmatch(r"[0-9]{1,4}", old.text))]
            words.append(word)
        improved = PageContent(page_number, content.width, content.height,
                               _group_words(words, content.width, content.height), "ocr")
        self._cache[page_number] = improved
        return improved

    def footer_labels(self, pdf, page_number: int) -> list[tuple[str, int]]:
        """Return Arabic/Roman labels from the page edges, with no body-number guesses."""
        page = pdf.pages[page_number - 1]
        width, height = float(page.width), float(page.height)
        words = _pdfplumber_words(page)
        content = PageContent(page_number, width, height, _group_words(words, width, height), "text")
        labels = _labels_in_edges(content)
        if labels or self.ocr == "off":
            return labels
        # A narrow crop makes small printed numbers easier for Windows OCR.
        with tempfile.TemporaryDirectory(prefix="pdf-bookmarker-footer-") as temporary:
            whole = Path(temporary) / "page.png"
            cropped = Path(temporary) / "edges.png"
            self._render(page_number, whole, scale=1700)
            with Image.open(whole) as image:
                band = max(1, int(image.height * 0.17))
                top = image.crop((0, 0, image.width, band))
                bottom = image.crop((0, image.height - band, image.width, image.height))
                combined = Image.new("RGB", (image.width, band * 2 + 20), "white")
                combined.paste(top, (0, 0))
                combined.paste(bottom, (0, band + 20))
                combined.save(cropped)
            raw = self._run_ocr(cropped)
        found: list[tuple[str, int]] = []
        for line in raw.get("lines", []):
            candidate = _parse_page_label(clean_text(line.get("text", "")))
            if candidate:
                found.append(candidate)
                continue
            words = line.get("words", [])
            for word in (words[:1] + words[-1:] if words else []):
                x = float(word.get("x", 0))
                if x < float(raw["width"]) * 0.2 or x > float(raw["width"]) * 0.78:
                    candidate = _parse_page_label(str(word.get("text", "")))
                    if candidate:
                        found.append(candidate)
        return list(dict.fromkeys(found))

    def _render(self, page_number: int, destination: Path, *, scale: int) -> None:
        executable = find_executable("pdftoppm")
        if not executable:
            raise RuntimeError("pdftoppm was not found in the app or PATH. Install Poppler and add it to PATH.")
        prefix = destination.with_suffix("")
        process = subprocess.run(
            [executable, "-f", str(page_number), "-l", str(page_number),
             "-scale-to", str(scale), "-png", "-singlefile", str(self.pdf_path), str(prefix)],
            capture_output=True, text=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if process.returncode or not destination.exists():
            raise RuntimeError(f"PDF page rendering failed: {process.stderr.strip()[:500]}")

    def _run_ocr_language(self, image_path: Path, language: str) -> dict:
        if self.ocr not in {"auto", "windows"}:
            raise RuntimeError(f"Unsupported OCR engine: {self.ocr}")
        powershell = find_executable("powershell.exe")
        if not powershell:
            raise RuntimeError("Windows PowerShell OCR is unavailable. Use Windows 10/11 with the Chinese OCR language pack.")
        script = ocr_script_path()
        process = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(script), "-ImagePath", str(image_path), "-Language", language],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if process.returncode:
            raise RuntimeError(process.stderr.strip()[:250])
        return json.loads(process.stdout.strip())

    def _run_ocr(self, image_path: Path) -> dict:
        errors: list[str] = []
        chinese: dict | None = None
        try:
            chinese = self._run_ocr_language(image_path, "zh-Hans-CN")
            text = " ".join(str(line.get("text", "")) for line in chinese.get("lines", []))
            cjk = sum("\u3400" <= character <= "\u9fff" for character in text)
            latin = sum("A" <= character <= "Z" or "a" <= character <= "z" for character in text)
            if cjk >= 4 and cjk >= latin * 0.25:
                return chinese
        except (RuntimeError, json.JSONDecodeError) as error:
            errors.append(str(error))
        try:
            english = self._run_ocr_language(image_path, "en-US")
            if english.get("lines") or chinese is None:
                return english
        except (RuntimeError, json.JSONDecodeError) as error:
            errors.append(str(error))
        if chinese is not None:
            return chinese
        raise RuntimeError("Windows OCR failed: " + "; ".join(errors))


_ARABIC_LABEL = re.compile(r"^[\s\[(（—–-]*([0-9]{1,4})[\s\])）—–-]*$")
_ROMAN_LABEL = re.compile(r"^[\s\[(（—–-]*([ivxlcdm]{1,8})[\s\])）—–-]*$", re.I)


def _roman_to_int(text: str) -> int:
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total = previous = 0
    for character in reversed(text.lower()):
        current = values[character]
        total += -current if current < previous else current
        previous = max(previous, current)
    return total


def _parse_page_label(text: str) -> tuple[str, int] | None:
    text = clean_text(text)
    match = _ARABIC_LABEL.fullmatch(text)
    if match:
        value = int(match.group(1))
        return ("arabic", value) if value > 0 else None
    match = _ROMAN_LABEL.fullmatch(text)
    if match:
        return ("roman", _roman_to_int(match.group(1)))
    return None


def _labels_in_edges(content: PageContent) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for line in content.lines:
        if line.y > content.height * 0.16 and line.bottom < content.height * 0.84:
            continue
        label = _parse_page_label(line.text)
        if label:
            found.append(label)
            continue
        if line.words:
            edge_tokens = (line.words[0], line.words[-1])
            for word in edge_tokens:
                if word.x < content.width * 0.2 or word.x > content.width * 0.78:
                    label = _parse_page_label(word.text)
                    if label:
                        found.append(label)
    return list(dict.fromkeys(found))
