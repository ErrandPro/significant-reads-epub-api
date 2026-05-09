def extract_rich_chapters_from_docx(
    docx_path: str,
) -> list[tuple[str, list[dict]]] | None:
    try:
        from docx import Document
        from docx.oxml.ns import qn
        from docx.text.paragraph import Paragraph as DocxPara
        from docx.table import Table as DocxTable

        doc = Document(docx_path)

        # ── 1. Determine body font size ────────────────────────────────────
        all_sizes: list[float] = []
        for para in doc.paragraphs:
            for run in para.runs:
                sz = _docx_run_size(run)
                if sz:
                    all_sizes.append(sz)

        body_size      = sorted(all_sizes)[len(all_sizes) // 2] if all_sizes else 12.0
        chapter_min_sz = body_size * 1.50
        section_min_sz = body_size * 1.15

        # ── 2. Walk state ──────────────────────────────────────────────────
        chapters:  list[tuple[str, list[dict]]] = []
        state:     dict = {"title": "Front Matter", "blocks": []}
        cur_lines: list[dict] = []
        cur_kind   = "text"

        def _flush_lines() -> None:
            nonlocal cur_lines, cur_kind
            if cur_lines:
                state["blocks"].append({"kind": cur_kind, "lines": cur_lines[:]})
                cur_lines = []
            cur_kind = "text"

        def _flush_chapter(new_title: str) -> None:
            _flush_lines()
            if state["blocks"]:
                chapters.append((state["title"], state["blocks"][:]))
            state["title"]  = new_title
            state["blocks"] = []

        def _para_heading_level(para) -> int:
            style_name = (para.style.name or "").lower().strip()
            text       = para.text.strip()

            # Title style → title page (handled separately, not a chapter split)
            if style_name == "title":
                return 0

            # Heading 1 → chapter boundary
            if style_name == "heading 1":
                return 1

            # Heading 7 is used in this book for "Chapter N" / section labels
            if style_name == "heading 7":
                return 1

            # Other heading styles → section heading
            if style_name.startswith("heading"):
                return 2

            # Size-based fallback
            if not text or len(text) > 140:
                return 0

            max_sz = _para_max_size(para, body_size)
            if max_sz >= chapter_min_sz:
                return 1
            if max_sz >= section_min_sz:
                return 2

            # Pattern-based chapter detection
            if is_chapter_heading(text):
                return 1

            # All-bold Normal paragraph = subheading
            if style_name == "normal" and len(text) <= 120:
                runs_with_text = [r for r in para.runs if r.text.strip()]
                if runs_with_text and all(r.bold for r in runs_with_text):
                    return 2

            return 0

        def _para_to_spans(para) -> list[dict]:
            spans = []
            for run in para.runs:
                txt = run.text
                if not txt:
                    continue
                spans.append({
                    "text":   txt,
                    "bold":   bool(run.bold),
                    "italic": bool(run.italic),
                    "size":   _docx_run_size(run) or body_size,
                })
            return spans

        # ── 3. Emit title page as first chapter ───────────────────────────
        title_page_lines = []
        for para in doc.paragraphs:
            sn = (para.style.name or "").lower().strip()
            if sn in ("heading 1", "heading 7"):
                break
            text = para.text.strip()
            if text:
                title_page_lines.append(text)

        if title_page_lines:
            book_title  = title_page_lines[0]
            front_lines = title_page_lines[1:]
            title_blocks = [{
                "kind": "text",
                "lines": [
                    {
                        "spans":      [{"text": t, "bold": False,
                                        "italic": False, "size": body_size}],
                        "is_section": False,
                    }
                    for t in front_lines
                ],
            }] if front_lines else []
            chapters.append((book_title, title_blocks))

        # ── 4. Collect floating text-boxes ────────────────────────────────
        sidebar_blocks: list[dict] = _extract_textboxes(doc, body_size)

        # ── 5. Iterate top-level body children ────────────────────────────
        for elem in doc.element.body:
            local = _local(elem.tag)

            if local == "p":
                para  = DocxPara(elem, doc)
                level = _para_heading_level(para)
                text  = para.text.strip()

                if level == 1 and text:
                    _flush_chapter(text)
                    continue

                if level == 2 and text:
                    _flush_lines()
                    max_sz = _para_max_size(para, body_size)
                    cur_lines.append({
                        "spans":      [{"text": text, "bold": True,
                                        "italic": False, "size": max_sz}],
                        "is_section": True,
                    })
                    _flush_lines()
                    continue

                img_blocks = _extract_inline_images(para)
                if img_blocks:
                    _flush_lines()
                    for ib in img_blocks:
                        state["blocks"].append(ib)

                spans = _para_to_spans(para)
                if spans:
                    cur_lines.append({"spans": spans, "is_section": False})

            elif local == "tbl":
                try:
                    table = DocxTable(elem, doc)
                    rows  = [
                        [c.text.strip() for c in row.cells]
                        for row in table.rows
                    ]
                    deduped = [_dedup_row(r) for r in rows if any(r)]
                    if deduped:
                        _flush_lines()
                        state["blocks"].append({"kind": "table", "rows": deduped})
                except Exception as e:
                    logger.debug(f"Table extraction error: {e}")
            else:
                continue

        _flush_lines()
        if state["blocks"]:
            chapters.append((state["title"], state["blocks"][:]))

        if sidebar_blocks and chapters:
            chapters[0][1].extend(sidebar_blocks)

        return chapters if chapters else None

    except Exception as e:
        logger.warning(f"extract_rich_chapters_from_docx failed: {e}", exc_info=True)
        return None
