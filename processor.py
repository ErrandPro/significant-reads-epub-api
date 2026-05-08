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
    Render first PDF page into PNG for EPUB cover.
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


# ── Column detection ─────────────────────────────────────────────────────────

def detect_columns(
    blocks: list[dict],
    page_width: float,
    min_gutter_width: float = 10.0,
    strip_count: int = 200,
) -> list[tuple[float, float]]:

    if not blocks or page_width <= 0:
        return [(0.0, page_width)]

    strip_width = page_width / strip_count
    occupancy = [0] * strip_count

    for blk in blocks:
        bx0, _, bx1, _ = blk.get("bbox", (0, 0, 0, 0))

        bx0 = max(0.0, bx0)
        bx1 = min(page_width, bx1)

        if bx1 <= bx0:
            continue

        si = int(bx0 / strip_width)
        ei = min(int(bx1 / strip_width), strip_count - 1)

        for s in range(si, ei + 1):
            occupancy[s] += 1

    if not any(occupancy):
        return [(0.0, page_width)]

    gutters = []
    in_gutter = False
    g_start = 0

    for i, occ in enumerate(occupancy):

        if occ == 0 and not in_gutter:
            in_gutter = True
            g_start = i

        elif occ > 0 and in_gutter:
            in_gutter = False

            g_x0 = g_start * strip_width
            g_x1 = i * strip_width

            if g_x1 - g_x0 >= min_gutter_width:
                gutters.append((g_x0, g_x1))

    if in_gutter:
        g_x0 = g_start * strip_width
        g_x1 = strip_count * strip_width

        if g_x1 - g_x0 >= min_gutter_width:
            gutters.append((g_x0, g_x1))

    if not gutters:
        return [(0.0, page_width)]

    col_boundaries = [0.0]

    for gx0, gx1 in gutters:
        col_boundaries.append(gx0)
        col_boundaries.append(gx1)

    col_boundaries.append(page_width)

    columns = []

    for i in range(0, len(col_boundaries) - 1, 2):
        cx0 = col_boundaries[i]
        cx1 = col_boundaries[i + 1]

        if cx1 - cx0 > min_gutter_width:
            columns.append((cx0, cx1))

    return columns if columns else [(0.0, page_width)]


def _block_column_index(
    blk_bbox: tuple,
    columns: list[tuple[float, float]],
    page_width: float,
) -> int:

    bx0, _, bx1, _ = blk_bbox

    if (bx1 - bx0) >= page_width * 0.80:
        return -1

    centre_x = (bx0 + bx1) / 2.0

    best_col = 0
    best_dist = float("inf")

    for i, (cx0, cx1) in enumerate(columns):
        dist = abs(centre_x - (cx0 + cx1) / 2.0)

        if dist < best_dist:
            best_dist = dist
            best_col = i

    return best_col


def sort_blocks_into_columns(
    blocks: list[dict],
    columns: list[tuple[float, float]],
    page_width: float,
) -> list[dict]:

    if len(columns) <= 1:
        return blocks

    full_width = []
    col_blocks = []

    for blk in blocks:
        bbox = blk.get("bbox", (0, 0, 0, 0))
        top_y = bbox[1]

        col_idx = _block_column_index(bbox, columns, page_width)

        if col_idx == -1:
            full_width.append((top_y, blk))
        else:
            col_blocks.append((col_idx, top_y, blk))

    col_blocks.sort(key=lambda t: (t[0], t[1]))

    sorted_col = [blk for _, _, blk in col_blocks]

    if not full_width:
        return sorted_col

    full_width.sort(key=lambda t: t[0])

    fw_iter = iter(full_width)
    next_fw = next(fw_iter, None)

    result = []

    for blk in sorted_col:
        blk_top_y = blk["bbox"][1]

        while next_fw is not None and next_fw[0] <= blk_top_y:
            result.append(next_fw[1])
            next_fw = next(fw_iter, None)

        result.append(blk)

    if next_fw is not None:
        result.append(next_fw[1])

    for _, fw_blk in fw_iter:
        result.append(fw_blk)

    return result


# ── Sidebar detection ────────────────────────────────────────────────────────

def _block_has_background(
    blk_bbox: tuple,
    page_drawings: list[dict],
    overlap_threshold: float = 0.55,
) -> bool:

    bx0, by0, bx1, by1 = blk_bbox

    blk_area = max((bx1 - bx0) * (by1 - by0), 1e-6)

    for d in page_drawings:

        fill = d.get("fill")
        color = d.get("color")

        has_fill = fill is not None and fill != (1, 1, 1)
        has_stroke = color is not None and color != (1, 1, 1)

        if not (has_fill or has_stroke):
            continue

        rect = d.get("rect")

        if rect is None:
            continue

        try:
            rx0, ry0, rx1, ry1 = (
                float(rect[0]),
                float(rect[1]),
                float(rect[2]),
                float(rect[3]),
            )

        except Exception:
            continue

        ix0 = max(bx0, rx0)
        iy0 = max(by0, ry0)
        ix1 = min(bx1, rx1)
        iy1 = min(by1, ry1)

        if ix1 <= ix0 or iy1 <= iy0:
            continue

        overlap = (ix1 - ix0) * (iy1 - iy0)

        if overlap / blk_area >= overlap_threshold:
            return True

    return False


def classify_sidebar(
    blk: dict,
    columns: list[tuple[float, float]],
    page_width: float,
    page_drawings: list[dict] | None = None,
    width_threshold: float = 0.45,
    offset_threshold: float = 0.15,
) -> bool:

    bbox = blk.get("bbox", (0, 0, 0, 0))

    bx0, _, bx1, _ = bbox

    blk_width = bx1 - bx0

    line_count = len(blk.get("lines", []))

    # Prevent paragraph-ending fragments from becoming sidebars
    if line_count <= 1:
        return False

    if blk_width >= page_width * 0.80:
        return False

    sig_width = blk_width < page_width * width_threshold

    centre_x = (bx0 + bx1) / 2.0

    min_col_dist = min(
        abs(centre_x - (cx0 + cx1) / 2.0)
        for cx0, cx1 in columns
    )

    sig_offset = min_col_dist > page_width * offset_threshold

    sig_background = False

    if page_drawings:
        sig_background = _block_has_background(bbox, page_drawings)

    score = sum([sig_width, sig_offset, sig_background])

    return score >= 2


# ── Image pairing ────────────────────────────────────────────────────────────

def find_text_image_pairs(
    blocks: list[dict],
    horiz_overlap_threshold: float = 0.25,
    vert_proximity_pts: float = 80.0,
) -> None:

    image_entries = [
        (i, blk["bbox"])
        for i, blk in enumerate(blocks)
        if blk.get("kind") == "image" and "bbox" in blk
    ]

    if not image_entries:
        return

    for txt_idx, blk in enumerate(blocks):

        if blk.get("kind") not in ("text", "sidebar"):
            continue

        tbbox = blk.get("bbox")

        if not tbbox:
            continue

        tx0, ty0, tx1, ty1 = tbbox

        for img_idx, ibbox in image_entries:

            if img_idx == txt_idx:
                continue

            ix0, iy0, ix1, iy1 = ibbox

            img_width = max(ix1 - ix0, 1e-6)

            horiz_overlap = max(
                0.0,
                min(tx1, ix1) - max(tx0, ix0)
            )

            if horiz_overlap / img_width < horiz_overlap_threshold:
                continue

            vert_gap = max(ty0 - iy1, iy0 - ty1, 0.0)

            if vert_gap > vert_proximity_pts:
                continue

            blk["nearby_image"] = True
            break


# ── Helpers ──────────────────────────────────────────────────────────────────

def _bbox_overlap(a: tuple, b: tuple, threshold: float = 0.25) -> bool:

    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b

    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)

    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)

    if ix1 <= ix0 or iy1 <= iy0:
        return False

    intersection = (ix1 - ix0) * (iy1 - iy0)

    area_a = max((ax1 - ax0) * (ay1 - ay0), 1e-6)

    return (intersection / area_a) > threshold


def _is_page_number(text: str) -> bool:
    s = text.strip()

    return bool(
        re.match(r"^\d{1,3}$", s)
        or re.match(r"^[ivxlcdmIVXLCDM]{1,8}$", s)
    )


def _is_cover_page(page) -> bool:

    blocks = page.get_text("dict")["blocks"]

    text_spans = []

    for blk in blocks:

        if blk.get("type") != 0:
            continue

        for ln in blk.get("lines", []):

            for sp in ln.get("spans", []):

                txt = sp["text"].strip()

                if txt:
                    text_spans.append((txt, sp["size"]))

    if not text_spans:
        return True

    long_lines = sum(1 for t, _ in text_spans if len(t.split()) > 8)
    large_text = sum(1 for _, sz in text_spans if sz > 20)

    return long_lines == 0 and large_text >= 2


def _is_title_page(page) -> bool:

    txt = page.get_text().lower()

    signals = [
        "copyright",
        "isbn",
        "all rights reserved",
        "published by",
    ]

    hits = sum(1 for s in signals if s in txt)

    return hits >= 2


def _is_toc_page(page, body_size: float) -> bool:

    raw_blocks = page.get_text("dict")["blocks"]

    hits = 0

    for blk in raw_blocks:

        if blk.get("type") != 0:
            continue

        for ln in blk.get("lines", []):

            spans = ln.get("spans", [])

            if not spans:
                continue

            text = " ".join(s["text"] for s in spans).strip()

            if not text:
                continue

            if re.search(r"\.{3,}\s*\d+$", text):
                hits += 1
                continue

            if re.search(r"\s{3,}[ivxlcdm\d]+\s*$", text, re.I):
                hits += 1
                continue

            if len(spans) >= 2:

                first = spans[0]
                last = spans[-1]

                last_text = last["text"].strip()

                if re.match(r"^[ivxlcdm\d]+$", last_text, re.I):

                    gap = last["bbox"][0] - first["bbox"][2]

                    if gap > 80:
                        hits += 1

    return hits >= 4


def _line_x0(line) -> float:

    spans = line.get("spans", [])

    if not spans:
        return 0

    return spans[0]["bbox"][0]


def _join_paragraph_lines(lines: list[dict]) -> list[dict]:

    if not lines:
        return []

    paragraphs = []

    current = {
        "spans": [],
        "is_section": False,
    }

    prev_text = ""
    prev_x0 = None

    for line in lines:

        text = " ".join(s["text"] for s in line["spans"]).strip()

        if not text:
            continue

        x0 = _line_x0({
            "spans": line["spans"]
        })

        starts_new_para = False

        if prev_x0 is None:
            starts_new_para = True

        elif abs(x0 - prev_x0) > 12:
            starts_new_para = True

        elif prev_text.endswith((".", "!", "?", ":")):
            starts_new_para = True

        if starts_new_para:

            if current["spans"]:
                paragraphs.append(current)

            current = {
                "spans": [],
                "is_section": line.get("is_section", False),
            }

            if "dropcap_char" in line:
                current["dropcap_char"] = line["dropcap_char"]

        current["spans"].extend(line["spans"])

        prev_text = text
        prev_x0 = x0

    if current["spans"]:
        paragraphs.append(current)

    return paragraphs


# ── Rich extraction ──────────────────────────────────────────────────────────

def extract_rich_chapters(pdf_path: str) -> list[tuple[str, list[dict]]] | None:

    try:
        import fitz

    except ImportError:
        logger.warning("PyMuPDF not available")
        return None

    try:
        doc = fitz.open(pdf_path)

        sizes = []

        for page in doc:

            for blk in page.get_text("dict")["blocks"]:

                if blk.get("type") != 0:
                    continue

                for ln in blk["lines"]:

                    for sp in ln["spans"]:

                        if sp["text"].strip():
                            sizes.append(sp["size"])

        if not sizes:
            doc.close()
            return None

        sizes.sort()

        body_size = sizes[len(sizes) // 2]

        chapter_min_size = body_size * 1.6
        section_min_size = body_size * 1.2

        chapters = []

        state = {
            "title": "Front Matter",
            "blocks": [],
            "lines": [],
        }

        seen_xrefs = set()

        pending_heading_parts = []

        pending_dropcap = None

        current_block_kind = "text"

        def _flush_lines():

            if state["lines"]:

                joined = _join_paragraph_lines(state["lines"])

                state["blocks"].append({
                    "kind": current_block_kind,
                    "lines": joined,
                })

                state["lines"] = []

        def _flush_pending_heading():

            nonlocal pending_heading_parts

            if not pending_heading_parts:
                return

            heading_text = " ".join(pending_heading_parts)

            pending_heading_parts = []

            _save_chapter(heading_text)

        def _save_chapter(new_title: str):

            _flush_lines()

            if state["blocks"]:
                chapters.append(
                    (state["title"], state["blocks"][:])
                )

            state["title"] = new_title
            state["blocks"] = []
            state["lines"] = []

        for page in doc:

            if page.number == 0 and _is_cover_page(page):
                logger.debug("Skipping cover page")
                continue

            if page.number <= 2 and _is_title_page(page):
                logger.debug("Skipping title page")
                continue

            if _is_toc_page(page, body_size):
                logger.debug(f"Skipping TOC page {page.number}")
                continue

            page_width = page.rect.width

            try:
                page_drawings = page.get_drawings()

            except Exception:
                page_drawings = []

            raw_blocks = page.get_text("dict", sort=True)["blocks"]

            columns = detect_columns(raw_blocks, page_width)

            ordered_blocks = sort_blocks_into_columns(
                raw_blocks,
                columns,
                page_width,
            )

            for blk in ordered_blocks:

                btype = blk.get("type")

                bbox = tuple(blk.get("bbox", (0, 0, 0, 0)))

                if btype == 1:

                    xref = blk.get("xref", 0)

                    if xref and xref not in seen_xrefs:

                        try:
                            img = doc.extract_image(xref)

                            w = img.get("width", 0)
                            h = img.get("height", 0)

                            if w >= 80 and h >= 80:

                                _flush_pending_heading()
                                _flush_lines()

                                state["blocks"].append({
                                    "kind": "image",
                                    "data": img["image"],
                                    "ext": img.get("ext", "png"),
                                    "width": w,
                                    "height": h,
                                    "bbox": bbox,
                                })

                                seen_xrefs.add(xref)

                        except Exception as e:
                            logger.debug(f"Image extraction error: {e}")

                    continue

                if btype != 0:
                    continue

                current_block_kind = (
                    "sidebar"
                    if classify_sidebar(
                        blk,
                        columns,
                        page_width,
                        page_drawings,
                    )
                    else "text"
                )

                for ln in blk.get("lines", []):

                    spans = ln.get("spans", [])

                    line_text = " ".join(
                        s["text"] for s in spans
                    ).strip()

                    if not line_text:
                        continue

                    if _is_page_number(line_text):
                        continue

                    max_size = max(
                        (s["size"] for s in spans),
                        default=body_size,
                    )

                    is_very_large = max_size >= chapter_min_size

                    if is_very_large and len(line_text) <= 2:

                        if pending_heading_parts:
                            _flush_pending_heading()

                        pending_dropcap = line_text.strip()
                        continue

                    if len(line_text) < 120 and (
                        is_very_large
                        or is_chapter_heading(line_text)
                    ):

                        if pending_dropcap is not None:
                            pending_dropcap = None

                        pending_heading_parts.append(line_text)
                        continue

                    if pending_heading_parts:
                        _flush_pending_heading()

                    span_items = [
                        {
                            "text": s["text"],
                            "bold": bool(s.get("flags", 0) & 16),
                            "italic": bool(s.get("flags", 0) & 2),
                            "size": s["size"],
                        }
                        for s in spans
                        if s["text"].strip()
                    ]

                    if span_items:

                        line_dict = {
                            "spans": span_items,
                            "is_section": (
                                max_size >= section_min_size
                            ),
                        }

                        if pending_dropcap is not None:
                            line_dict["dropcap_char"] = pending_dropcap
                            pending_dropcap = None

                        state["lines"].append(line_dict)

        _flush_pending_heading()
        _flush_lines()

        if state["blocks"]:
            chapters.append(
                (state["title"], state["blocks"][:])
            )

        doc.close()

        if not chapters:
            return None

        return chapters

    except Exception as e:
        logger.warning(f"extract_rich_chapters failed: {e}")
        return None


# ── Text cleaning ────────────────────────────────────────────────────────────

def detect_running_headers(lines: list[str]) -> set[str]:

    stripped = [l.strip() for l in lines if l.strip()]

    freq = Counter(stripped)

    return {
        line
        for line, count in freq.items()
        if count >= 3 and len(line) > 4
    }


def clean_text(text: str) -> str:

    text = re.sub(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]",
        "",
        text,
    )

    lines = text.split("\n")

    running_headers = detect_running_headers(lines)

    cleaned = []

    for line in lines:

        s = line.strip()

        if re.match(r"^\d{1,3}$", s):
            continue

        if re.match(r"^[ivxlcdmIVXLCDM]{1,6}$", s):
            continue

        if s in running_headers:
            continue

        cleaned.append(line)

    return "\n".join(cleaned)


# ── Chapter detection ────────────────────────────────────────────────────────

def skip_toc(lines: list[str]) -> int:

    toc_pattern = re.compile(
        r"\.{3,}|\s{2,}\d+\s*$"
    )

    toc_hits = 0
    last_toc_line = 0

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
            r"^(introduction|foreword|preface|dedication|"
            r"acknowledgements?|endorsements?|"
            r"conclusion|epilogue|prologue)$",
            re.IGNORECASE,
        ),

        re.compile(
            r"^chapter\s+(one|two|three|four|five|"
            r"six|seven|eight|nine|ten)\b",
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

    upper_ratio = (
        sum(1 for c in alpha if c.isupper())
        / len(alpha)
    )

    return upper_ratio > 0.85 and len(s.split()) >= 2


def extract_chapters_from_text(
    text: str
) -> list[tuple[str, str]]:

    text = clean_text(text)

    lines = text.split("\n")

    start_idx = skip_toc(lines)

    lines = lines[start_idx:]

    chapters = []

    current_title = "Front Matter"

    current_content = []

    i = 0

    while i < len(lines):

        line = lines[i]
        s = line.strip()

        if not s:
            current_content.append("")
            i += 1
            continue

        if is_chapter_heading(s):

            content_text = "\n".join(
                current_content
            ).strip()

            if content_text:
                chapters.append(
                    (current_title, content_text)
                )

            current_title = s
            current_content = []

            i += 1
            continue

        elif is_section_heading(s):

            current_content.extend([
                "",
                line,
                "",
            ])

            i += 1
            continue

        current_content.append(line)

        i += 1

    content_text = "\n".join(
        current_content
    ).strip()

    if content_text:
        chapters.append(
            (current_title, content_text)
        )

    return chapters if chapters else split_by_wordcount(text)


def split_by_wordcount(
    text: str,
    words_per_part: int = 800
) -> list[tuple[str, str]]:

    words = text.split()

    parts = []

    for part_num, i in enumerate(
        range(0, len(words), words_per_part),
        start=1,
    ):

        chunk = " ".join(
            words[i:i + words_per_part]
        )

        parts.append(
            (f"Part {part_num}", chunk)
        )

    return parts if parts else [("Content", text)]
