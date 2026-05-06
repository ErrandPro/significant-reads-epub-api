import re
import logging
from collections import Counter

from pdfminer.high_level import extract_text

logger = logging.getLogger(__name__)


# ── Text extraction ──────────────────────────────────────────────────────────

def extract_text_from_pdf(pdf_path: str) -> str:
    """pdfminer extraction — good fallback for complex fonts."""
    try:
        text = extract_text(pdf_path)
        return text if text else ""
    except Exception as e:
        logger.warning(f"pdfminer failed: {e}")
        return ""


def ocr_pdf_if_needed(pdf_path: str) -> str:
    """
    Tesseract OCR pipeline — last resort for image-only PDFs.
    Uses pdf2image + pytesseract with basic pre-processing.
    """
    from pdf2image import convert_from_path
    import pytesseract
    from PIL import ImageFilter, ImageOps

    images = convert_from_path(pdf_path, dpi=200)
    pages = []
    for img in images:
        # Light pre-processing improves OCR accuracy on low-quality scans
        img = img.convert("L")        # Greyscale
        img = ImageOps.autocontrast(img)
        img = img.filter(ImageFilter.SHARPEN)
        pages.append(pytesseract.image_to_string(img, lang="eng"))
    return "\n".join(pages)


def extract_cover_image(pdf_path: str) -> bytes | None:
    """
    Try to pull the first-page rendered image as a cover for the EPUB.
    Returns PNG bytes or None if extraction fails.
    """
    try:
        import fitz
        doc = fitz.open(pdf_path)
        page = doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
        png_bytes = pix.tobytes("png")
        doc.close()
        return png_bytes
    except Exception as e:
        logger.info(f"Cover extraction skipped: {e}")
        return None


# ── Text cleaning ─────────────────────────────────────────────────────────────

def detect_running_headers(lines: list[str]) -> set[str]:
    """Find lines that repeat 3+ times — page headers/footers."""
    stripped = [l.strip() for l in lines if l.strip()]
    freq = Counter(stripped)
    return {line for line, count in freq.items() if count >= 3 and len(line) > 4}


def clean_text(text: str) -> str:
    """Remove control chars, running headers, standalone page numbers."""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    lines = text.split("\n")
    running_headers = detect_running_headers(lines)

    cleaned = []
    for line in lines:
        s = line.strip()
        if re.match(r"^\d{1,3}$", s) or re.match(r"^[ivxlcdmIVXLCDM]{1,6}$", s):
            continue
        if s in running_headers:
            continue
        for header in running_headers:
            if s.startswith(header) and len(s) > len(header):
                line = s[len(header):].strip()
                s = line
                break
        cleaned.append(line)

    return "\n".join(cleaned)


# ── Chapter detection ─────────────────────────────────────────────────────────

def skip_toc(lines: list[str]) -> int:
    toc_pattern = re.compile(r"\.{3,}|\s{2,}\d+\s*$")
    toc_hits, last_toc_line = 0, 0
    for i, line in enumerate(lines[:150]):
        if toc_pattern.search(line.strip()):
            toc_hits += 1
            last_toc_line = i
    return last_toc_line + 3 if toc_hits >= 3 else 0


def is_chapter_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 100:
        return False
    patterns = [
        re.compile(r"^chapter\s+\d+", re.IGNORECASE),
        re.compile(
            r"^(introduction|foreword|preface|dedication|acknowledgements?|"
            r"endorsements?|conclusion|epilogue|prologue)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^chapter\s+(one|two|three|four|five|six|seven|eight|nine|ten)\b",
            re.IGNORECASE,
        ),
    ]
    return any(p.match(s) for p in patterns)


def is_section_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 80 or len(s) < 4:
        return False
    alpha = [c for c in s if c.isalpha()]
    if not alpha:
        return False
    upper_ratio = sum(1 for c in alpha if c.isupper()) / len(alpha)
    return upper_ratio > 0.85 and len(s.split()) >= 2


def extract_chapters_from_text(text: str) -> list[tuple[str, str]]:
    text = clean_text(text)
    lines = text.split("\n")
    start_idx = skip_toc(lines)
    lines = lines[start_idx:]

    chapters: list[tuple[str, str]] = []
    current_title = "Front Matter"
    current_content: list[str] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()

        if not s:
            current_content.append("")
            i += 1
            continue

        if is_chapter_heading(s):
            content_text = "\n".join(current_content).strip()
            if content_text:
                chapters.append((current_title, content_text))

            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1

            subtitle = ""
            if j < len(lines):
                next_s = lines[j].strip()
                if (
                    next_s
                    and not is_chapter_heading(next_s)
                    and len(next_s) < 70
                    and len(next_s.split()) <= 7
                    and next_s[0].isupper()
                ):
                    subtitle = next_s
                    i = j + 1
                else:
                    i += 1
            else:
                i += 1

            current_title = f"{s}: {subtitle}" if subtitle else s
            current_content = []
            continue

        elif is_section_heading(s):
            current_content.extend(["", line, ""])
            i += 1
            continue

        current_content.append(line)
        i += 1

    content_text = "\n".join(current_content).strip()
    if content_text:
        chapters.append((current_title, content_text))

    if chapters:
        sizes = [len(c.split()) for _, c in chapters]
        max_size = max(sizes)
        tiny = sum(1 for sz in sizes if sz < 30)
        if tiny > len(chapters) * 0.5 and max_size > 2000:
            return split_by_wordcount(text)

    return chapters if chapters else split_by_wordcount(text)


def split_by_wordcount(text: str, words_per_part: int = 800) -> list[tuple[str, str]]:
    words = text.split()
    parts = []
    for part_num, i in enumerate(range(0, len(words), words_per_part), start=1):
        chunk = " ".join(words[i : i + words_per_part])
        parts.append((f"Part {part_num}", chunk))
    return parts if parts else [("Content", text)]
