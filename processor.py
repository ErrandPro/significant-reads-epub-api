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
        img = img.convert("L")
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


# ── Rich extraction (images + tables + formatting) ────────────────────────────

def _bbox_overlap(a: tuple, b: tuple, threshold: float = 0.25) -> bool:
    """Return True if bbox a overlaps bbox b by more than threshold of a's area."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return False
    intersection = (ix1 - ix0) * (iy1 - iy0)
    area_a = max((ax1 - ax0) * (ay1 - ay0), 1e-6)
    return (intersection / area_a) > threshold


def extract_rich_chapters(pdf_path: str) -> list[tuple[str, list[dict]]] | None:
    """
    Extract chapters as rich content using PyMuPDF.

    Each chapter is a (title, blocks) tuple where each block is one of:
      {"kind": "text",  "lines": [{"spans": [{"text", "bold", "italic", "size"}],
                                    "is_section": bool}]}
      {"kind": "image", "data": bytes, "ext": str, "width": int, "height": int}
      {"kind": "table", "rows": [[str, ...]]}

    Uses font-size analysis for heading detection (falls back to regex patterns).
    Returns None when PyMuPDF is unavailable or chapter quality is poor.
    """
    try:
        import fitz
    except ImportError:
        logger.warning("PyMuPDF not available — skipping rich extraction")
        return None

    try:
        doc = fitz.open(pdf_path)

        # ── Pass 1: determine median body font size ──────────────────────
        sizes: list[float] = []
        for page in doc:
            for blk in page.get_text("dict")["blocks"]:
                if blk.get("type") == 0:
                    for ln in blk["lines"]:
                        for sp in ln["spans"]:
                            if sp["text"].strip():
                                sizes.append(sp["size"])
        if not sizes:
            doc.close()
            return None

        sizes.sort()
        body_size: float = sizes[len(sizes) // 2]
        chapter_min_size: float = body_size * 1.6   # very large → chapter title
        section_min_size: float = body_size * 1.2   # moderately large → section heading

        # ── Pass 2: walk every page ──────────────────────────────────────
        chapters: list[tuple[str, list[dict]]] = []
        state: dict = {"title": "Front Matter", "blocks": [], "lines": []}
        seen_xrefs: set[int] = set()

        def _flush_lines() -> None:
            if state["lines"]:
                state["blocks"].append({"kind": "text", "lines": state["lines"][:]})
                state["lines"] = []

        def _save_chapter() -> None:
            _flush_lines()
            if state["blocks"]:
                chapters.append((state["title"], state["blocks"][:]))
            state["blocks"] = []
            state["lines"] = []

        for page in doc:
            # ── Tables ───────────────────────────────────────────────────
            tab_regions: list[tuple[tuple, list[list[str]]]] = []
            try:
                for tab in page.find_tables().tables:
                    raw = tab.extract()
                    rows = [
                        [str(c).strip() if c else "" for c in row]
                        for row in raw if any(c for c in row)
                    ]
                    if len(rows) >= 2:           # need at least header + 1 data row
                        tab_regions.append((tuple(tab.bbox), rows))
            except Exception as e:
                logger.debug(f"Table extraction skipped on a page: {e}")

            added_tab_indices: set[int] = set()

            # ── Text and image blocks ────────────────────────────────────
            for blk in page.get_text("dict", sort=True)["blocks"]:
                btype = blk.get("type")
                bbox  = tuple(blk.get("bbox", (0, 0, 0, 0)))

                # Table overlap — emit table block once, skip overlapping text
                tab_hit = next(
                    (
                        (i, trows)
                        for i, (tbbox, trows) in enumerate(tab_regions)
                        if i not in added_tab_indices and _bbox_overlap(bbox, tbbox)
                    ),
                    None,
                )
                if tab_hit is not None:
                    i, trows = tab_hit
                    _flush_lines()
                    state["blocks"].append({"kind": "table", "rows": trows})
                    added_tab_indices.add(i)
                    continue

                # Image block
                if btype == 1:
                    xref = blk.get("xref", 0)
                    if xref and xref not in seen_xrefs:
                        try:
                            img = doc.extract_image(xref)
                            w, h = img.get("width", 0), img.get("height", 0)
                            if w >= 80 and h >= 80:          # skip tiny decoratives
                                _flush_lines()
                                state["blocks"].append({
                                    "kind":   "image",
                                    "data":   img["image"],
                                    "ext":    img.get("ext", "png"),
                                    "width":  w,
                                    "height": h,
                                })
                                seen_xrefs.add(xref)
                        except Exception as e:
                            logger.debug(f"Image extraction error: {e}")
                    continue

                # Text block
                if btype != 0:
                    continue

                for ln in blk.get("lines", []):
                    spans = ln.get("spans", [])
                    line_text = " ".join(s["text"] for s in spans).strip()
                    if not line_text:
                        continue

                    max_size = max((s["size"] for s in spans), default=body_size)
                    is_very_large = max_size >= chapter_min_size

                    # Chapter heading detection (font size OR regex pattern)
                    if len(line_text) < 120 and (
                        is_very_large or is_chapter_heading(line_text)
                    ):
                        _save_chapter()
                        state["title"] = line_text
                        continue

                    # Build span list with formatting flags
                    span_items = [
                        {
                            "text":   s["text"],
                            "bold":   bool(s.get("flags", 0) & 16),
                            "italic": bool(s.get("flags", 0) & 2),
                            "size":   s["size"],
                        }
                        for s in spans
                        if s["text"].strip()
                    ]
                    if span_items:
                        state["lines"].append({
                            "spans":      span_items,
                            "is_section": max_size >= section_min_size,
                        })

        _save_chapter()
        doc.close()

        if not chapters:
            return None

        # ── Quality gate ─────────────────────────────────────────────────
        word_counts = [
            sum(
                len(s["text"].split())
                for blk in blocks if blk["kind"] == "text"
                for ln in blk["lines"]
                for s in ln["spans"]
            )
            for _, blocks in chapters
        ]
        max_words = max(word_counts, default=0)
        tiny_chapters = sum(1 for w in word_counts if w < 25)
        if tiny_chapters > len(chapters) * 0.55 and max_words > 500:
            logger.info("Rich extraction: too many tiny chapters — falling back to text pipeline")
            return None

        logger.info(
            f"Rich extraction: {len(chapters)} chapters, "
            f"{sum(word_counts)} words total"
        )
        return chapters

    except Exception as e:
        logger.warning(f"extract_rich_chapters failed: {e}")
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
