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


# ── Phase 1: Column detection & reading-order reconstruction ─────────────────
#
# Strategy: vertical projection profile.
#
# For every text block on a page we know its left (x0) and right (x1) edge.
# We slice the page width into thin vertical strips and count how many blocks
# "occupy" each strip (i.e. their bbox spans across it).  A strip that is
# consistently UNoccupied across many blocks is a column gutter.
#
# Once gutters are found we assign each block a column index (0, 1, 2 …) and
# re-sort by (column_index, top_y).  Full-width blocks (wider than 80 % of
# page width) are treated as spanning headers/footers and interleaved by y.

def detect_columns(
    blocks: list[dict],
    page_width: float,
    min_gutter_width: float = 10.0,
    strip_count: int = 200,
) -> list[tuple[float, float]]:
    """
    Analyse block bboxes and return a list of (col_x0, col_x1) column bands
    sorted left-to-right.

    Parameters
    ----------
    blocks            Raw PyMuPDF block dicts (type 0 or 1) for one page.
    page_width        Page width in points (page.rect.width).
    min_gutter_width  Minimum gap width (pts) to count as a real column gutter.
    strip_count       Resolution of the projection profile (200 is plenty for
                      A4/letter at sub-millimetre precision).

    Returns
    -------
    List of (x0, x1) tuples, one per detected column, left-to-right.
    Falls back to [(0, page_width)] (single column) when no gutter is found.
    """
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

    gutters: list[tuple[float, float]] = []
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

    col_boundaries: list[float] = [0.0]
    for gx0, gx1 in gutters:
        col_boundaries.append(gx0)
        col_boundaries.append(gx1)
    col_boundaries.append(page_width)

    columns: list[tuple[float, float]] = []
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
    """
    Return the 0-based column index that best contains this block's horizontal
    centre.  Blocks spanning >80 % of page width return -1 (full-width).
    """
    bx0, _, bx1, _ = blk_bbox
    if (bx1 - bx0) >= page_width * 0.80:
        return -1

    centre_x = (bx0 + bx1) / 2.0
    best_col, best_dist = 0, float("inf")
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
    """
    Re-order blocks into correct reading order for a multi-column layout.

    Single-column pages are returned unchanged (fast path).
    """
    if len(columns) <= 1:
        return blocks

    full_width: list[tuple[float, dict]] = []
    col_blocks: list[tuple[int, float, dict]] = []

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
    next_fw: tuple | None = next(fw_iter, None)

    result: list[dict] = []
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


# ── Phase 2: Sidebar detection ───────────────────────────────────────────────
#
# A sidebar is a text block that sits *beside* the main reading flow rather
# than within it.  Three independent signals are combined; a block needs at
# least two of the three to be classified as a sidebar, reducing false
# positives on narrow pull-quotes or short body paragraphs.
#
# Signal 1 — WIDTH
#   The block is narrower than 45 % of the page width.  A full body column
#   (even in a two-column layout) typically fills at least 45 % of the page.
#
# Signal 2 — HORIZONTAL OFFSET FROM COLUMN CENTRES
#   Every detected body column has a centre x.  If the block's own centre x
#   is further than 15 % of page_width away from *every* column centre it is
#   laterally displaced — i.e. it occupies an interstitial position that body
#   text never uses.
#
# Signal 3 — BACKGROUND SHADING OR BORDER (PyMuPDF drawings check)
#   PDFs often mark sidebars with a filled rectangle or a ruled border behind
#   or around the text block.  We check whether any filled/stroked drawing
#   rect on the page substantially overlaps the block's bbox.  This signal is
#   optional (the drawings list may be empty on some PDFs) but when present it
#   is very reliable.
#
# The function is designed to be called *before* the lines of a block are
# processed so that the caller can route the block to the sidebar path.

def _block_has_background(
    blk_bbox: tuple,
    page_drawings: list[dict],
    overlap_threshold: float = 0.55,
) -> bool:
    """
    Return True if a filled or stroked drawing rect on the page overlaps
    the block's bbox by at least `overlap_threshold` of the block's area.

    `page_drawings` is the list returned by page.get_drawings() in PyMuPDF.
    Only rects with a fill colour or a stroke colour distinct from white/none
    are considered.
    """
    bx0, by0, bx1, by1 = blk_bbox
    blk_area = max((bx1 - bx0) * (by1 - by0), 1e-6)

    for d in page_drawings:
        # Only filled or visibly stroked rects
        fill   = d.get("fill")
        color  = d.get("color")
        has_fill   = fill  is not None and fill  != (1, 1, 1)   # not white
        has_stroke = color is not None and color != (1, 1, 1)
        if not (has_fill or has_stroke):
            continue

        rect = d.get("rect")
        if rect is None:
            continue

        # rect can be a fitz.Rect or a plain tuple
        try:
            rx0, ry0, rx1, ry1 = float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])
        except (TypeError, IndexError):
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
    """
    Return True if *blk* is a sidebar block that should be rendered outside
    the main paragraph flow.

    Parameters
    ----------
    blk               A PyMuPDF block dict (type 0, text).
    columns           Column bands from detect_columns() for this page.
    page_width        Page width in points.
    page_drawings     page.get_drawings() list; pass None to skip Signal 3.
    width_threshold   Block must be narrower than this fraction of page_width.
    offset_threshold  Block centre must be further than this fraction of
                      page_width from every column centre to fire Signal 2.

    Returns
    -------
    True when at least 2 of the 3 signals are positive.
    Always False for full-width blocks (they are chapter/section headings).
    """
    bbox = blk.get("bbox", (0, 0, 0, 0))
    bx0, _, bx1, _ = bbox
    blk_width = bx1 - bx0

    # Full-width blocks are never sidebars
    if blk_width >= page_width * 0.80:
        return False

    # ── Signal 1: width ──────────────────────────────────────────────────
    sig_width = blk_width < page_width * width_threshold

    # ── Signal 2: lateral offset from all column centres ────────────────
    centre_x = (bx0 + bx1) / 2.0
    min_col_dist = min(
        abs(centre_x - (cx0 + cx1) / 2.0)
        for cx0, cx1 in columns
    )
    sig_offset = min_col_dist > page_width * offset_threshold

    # ── Signal 3: background shading / border ────────────────────────────
    sig_background = False
    if page_drawings:
        sig_background = _block_has_background(bbox, page_drawings)

    score = sum([sig_width, sig_offset, sig_background])
    if score >= 2:
        logger.debug(
            f"Sidebar detected at bbox={bbox} "
            f"(width={sig_width}, offset={sig_offset}, bg={sig_background})"
        )
        return True
    return False


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


def _is_toc_page(page, body_size: float) -> bool:
    """
    Heuristic: a page is a Table of Contents page if it contains several
    lines that end with a page-number separated by dot leaders or wide
    whitespace.
    """
    toc_pattern = re.compile(r"\.{3,}|\s{3,}\d+\s*$")
    raw_lines = page.get_text().splitlines()
    hits = sum(1 for ln in raw_lines if toc_pattern.search(ln.strip()))
    return hits >= 4


def extract_rich_chapters(pdf_path: str) -> list[tuple[str, list[dict]]] | None:
    """
    Extract chapters as rich content using PyMuPDF.

    Each chapter is a (title, blocks) tuple where each block is one of:
      {"kind": "text",    "lines": [{"spans": [...], "is_section": bool}]}
      {"kind": "sidebar", "lines": [{"spans": [...], "is_section": bool}]}
      {"kind": "image",   "data": bytes, "ext": str, "width": int, "height": int}
      {"kind": "table",   "rows": [[str, ...]]}

    Phase 2 additions (sidebar detection)
    ───────────────────────────────────────
    For every text block, classify_sidebar() is called with the page's column
    bands and drawing list before lines are accumulated.  Blocks that score
    positive are tagged "kind": "sidebar" instead of "kind": "text".  The line
    and span structure inside is identical so the renderer can reuse the same
    span-rendering logic with different wrapper HTML.

    Phase 1 additions (column layout) — unchanged:
      detect_columns() + sort_blocks_into_columns() run on every page.

    Previously fixed bugs — unchanged:
      1. _flush_lines() before every text block → one <p> per block.
      2. TOC pages skipped → no spurious chapters.
      3. Multi-line headings accumulated → joined into one title.
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
        chapter_min_size: float = body_size * 1.6
        section_min_size: float = body_size * 1.2

        # ── Pass 2: walk every page ──────────────────────────────────────
        chapters: list[tuple[str, list[dict]]] = []
        state: dict = {"title": "Front Matter", "blocks": [], "lines": []}
        seen_xrefs: set[int] = set()
        pending_heading_parts: list[str] = []

        # Carries the kind for the block currently being accumulated.
        # Switches between "text" and "sidebar" at the start of each block.
        current_block_kind: str = "text"

        def _flush_lines() -> None:
            if state["lines"]:
                state["blocks"].append({
                    "kind":  current_block_kind,
                    "lines": state["lines"][:],
                })
                state["lines"] = []

        def _flush_pending_heading() -> None:
            nonlocal pending_heading_parts
            if not pending_heading_parts:
                return
            heading_text = " ".join(pending_heading_parts)
            pending_heading_parts = []
            _save_chapter(heading_text)

        def _save_chapter(new_title: str) -> None:
            _flush_lines()
            if state["blocks"]:
                chapters.append((state["title"], state["blocks"][:]))
            state["title"]  = new_title
            state["blocks"] = []
            state["lines"]  = []

        for page in doc:
            if _is_toc_page(page, body_size):
                logger.debug(f"Skipping TOC page {page.number}")
                continue

            page_width: float = page.rect.width

            # Phase 2: collect drawings once per page for Signal 3
            try:
                page_drawings: list[dict] = page.get_drawings()
            except Exception:
                page_drawings = []

            # ── Tables ───────────────────────────────────────────────────
            tab_regions: list[tuple[tuple, list[list[str]]]] = []
            try:
                for tab in page.find_tables().tables:
                    raw = tab.extract()
                    rows = [
                        [str(c).strip() if c else "" for c in row]
                        for row in raw if any(c for c in row)
                    ]
                    if len(rows) >= 2:
                        tab_regions.append((tuple(tab.bbox), rows))
            except Exception as e:
                logger.debug(f"Table extraction skipped on a page: {e}")

            added_tab_indices: set[int] = set()

            # ── Phase 1: column-aware block ordering ─────────────────────
            raw_blocks: list[dict] = page.get_text("dict", sort=True)["blocks"]
            columns: list[tuple[float, float]] = detect_columns(raw_blocks, page_width)
            ordered_blocks: list[dict] = sort_blocks_into_columns(
                raw_blocks, columns, page_width
            )

            if len(columns) > 1:
                logger.debug(
                    f"Page {page.number}: {len(columns)}-column layout detected "
                    f"({[f'{c[0]:.0f}–{c[1]:.0f}' for c in columns]})"
                )

            # ── Text and image blocks ────────────────────────────────────
            for blk in ordered_blocks:
                btype = blk.get("type")
                bbox  = tuple(blk.get("bbox", (0, 0, 0, 0)))

                # Table overlap
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
                    _flush_pending_heading()
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
                            if w >= 80 and h >= 80:
                                _flush_pending_heading()
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

                # Bug 1 fix: flush previous block → its own <p>
                _flush_lines()

                # ── Phase 2: classify sidebar before processing lines ────
                nonlocal_kind = ["text"]   # mutable cell so _flush_lines can read it
                if classify_sidebar(blk, columns, page_width, page_drawings):
                    nonlocal_kind[0] = "sidebar"
                current_block_kind = nonlocal_kind[0]

                for ln in blk.get("lines", []):
                    spans = ln.get("spans", [])
                    line_text = " ".join(s["text"] for s in spans).strip()
                    if not line_text:
                        continue

                    max_size = max((s["size"] for s in spans), default=body_size)
                    is_very_large = max_size >= chapter_min_size

                    # Bug 3 fix: accumulate consecutive large-font lines.
                    # Sidebar blocks are unlikely to be chapter headings, but
                    # we honour the check anyway for robustness.
                    if len(line_text) < 120 and (
                        is_very_large or is_chapter_heading(line_text)
                    ):
                        pending_heading_parts.append(line_text)
                        continue

                    if pending_heading_parts:
                        _flush_pending_heading()

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

        # End of document
        _flush_pending_heading()
        _flush_lines()
        if state["blocks"]:
            chapters.append((state["title"], state["blocks"][:]))

        doc.close()

        if not chapters:
            return None

        # ── Quality gate ─────────────────────────────────────────────────
        word_counts = [
            sum(
                len(s["text"].split())
                for blk in blocks if blk["kind"] in ("text", "sidebar")
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
