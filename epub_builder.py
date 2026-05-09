import os
import re
import zipfile

from processor import extract_chapters_from_text, extract_cover_image

SKIP_CHAPTERS = {"contents", "table of contents", "content"}


# ── Shared helpers ────────────────────────────────────────────────────────────


def _sanitize(text: str) -> str:
    """Strip XML control chars and escape XML special chars."""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_CHAPTER_CSS = """
    body  { font-family: Arial, sans-serif; margin: 0pt 14pt;
            line-height: 140%; color: #000; font-size: 1.09em; }
    h1    { font-size: 1.4em;  font-weight: bold; margin: 28pt 0pt 14pt;
            text-align: center; line-height: 120%; }
    h2    { font-size: 1.15em; font-weight: bold; margin: 16pt 0pt 6pt; }
    p     { margin: 0pt 0pt 8.5pt; text-indent: 14pt;
            text-align: justify; line-height: 140%; widows: 0; orphans: 0; }
    strong { font-weight: bold; }
    em     { font-style: italic; }
    table  { width: 100%; border-collapse: collapse; margin: 1em 0;
             font-size: 0.95em; }
    th     { background: #f0f0f0; font-weight: bold; text-align: left;
             padding: 5pt 8pt; border: 1pt solid #ccc; }
    td     { padding: 4pt 8pt; border: 1pt solid #ddd; vertical-align: top; }
    .img-wrap { text-align: center; margin: 1em 0; }
    .img-wrap img { max-width: 95%; height: auto; }

    .sidebar {
        display: block;
        margin: 1.2em 0.5em 1.2em 1.5em;
        padding: 0.6em 0.9em;
        border-left: 3pt solid #4a7fa5;
        background-color: #f0f6fb;
        font-size: 0.93em;
        line-height: 145%;
    }
    .sidebar p {
        margin: 0pt 0pt 5pt;
        text-indent: 0;
        text-align: left;
    }
    .sidebar h2 {
        font-size: 1.05em;
        margin: 0pt 0pt 4pt;
        color: #2a5f85;
    }

    figure.float-image {
        display: block;
        margin: 1em auto;
        text-align: center;
        border: 1pt solid #d8d8d8;
        padding: 0.4em;
        background-color: #fafafa;
        max-width: 90%;
    }
    figure.float-image img {
        max-width: 100%;
        height: auto;
        display: block;
        margin: 0 auto;
    }
    figure.float-image figcaption {
        font-size: 0.85em;
        color: #555;
        margin-top: 0.3em;
        font-style: italic;
    }

    .dropcap {
        float: left;
        font-size: 3em;
        line-height: 0.85;
        margin: 0.05em 0.06em 0 0;
        font-weight: bold;
        color: #1a1a1a;
    }
"""


def _chapter_xhtml(chapter_title: str, body_html: str) -> str:
    safe_title = _sanitize(chapter_title)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <title>{safe_title}</title>
  <style>/* <![CDATA[ */{_CHAPTER_CSS}/* ]]> */</style>
</head>
<body>
  <h1>{safe_title}</h1>
  {body_html}
</body>
</html>"""


# ── Rich rendering ────────────────────────────────────────────────────────────


def _render_spans(spans: list[dict]) -> str:
    """Convert a list of span dicts to inline HTML with bold/italic tags."""
    parts: list[str] = []

    for s in spans:
        text = _sanitize(s.get("text", ""))

        if not text.strip():
            parts.append(text)
            continue

        b = s.get("bold", False)
        i = s.get("italic", False)

        if b and i:
            text = f"<strong><em>{text}</em></strong>"
        elif b:
            text = f"<strong>{text}</strong>"
        elif i:
            text = f"<em>{text}</em>"

        parts.append(text)

    return "".join(parts)


def _render_text_lines(lines: list[dict], html: list[str]) -> None:
    para_parts: list[str]      = []
    pending_dropcap_html: str | None = None

    for ln in lines:
        spans      = ln.get("spans", [])
        is_section = ln.get("is_section", False)
        rendered   = _render_spans(spans).strip()

        if not rendered:
            continue

        dropcap_char = ln.get("dropcap_char")

        if dropcap_char and pending_dropcap_html is None:
            safe_dc              = _sanitize(dropcap_char)
            pending_dropcap_html = f'<span class="dropcap">{safe_dc}</span>'

        if is_section:
            if para_parts:
                p_content = "".join(para_parts).strip()

                if pending_dropcap_html:
                    html.append(
                        f'<p style="text-indent:0">'
                        f'{pending_dropcap_html}{p_content}</p>'
                    )
                    pending_dropcap_html = None
                else:
                    html.append(f"<p>{p_content}</p>")

                para_parts = []

            html.append(f"<h2>{rendered}</h2>")

        else:
            para_parts.append(rendered + " ")

    if para_parts:
        p_content = "".join(para_parts).strip()

        if pending_dropcap_html:
            html.append(
                f'<p style="text-indent:0">'
                f'{pending_dropcap_html}{p_content}</p>'
            )
        else:
            html.append(f"<p>{p_content}</p>")


def _render_sidebar_block(blk: dict) -> str:
    inner: list[str] = []
    _render_text_lines(blk.get("lines", []), inner)

    if not inner:
        return ""

    return f'<div class="sidebar">\n{"".join(inner)}\n</div>'


def _render_rich_blocks(
    blocks: list[dict],
    images: dict[str, bytes],
    img_prefix: str,
) -> str:
    html: list[str]              = []
    img_idx                      = 0
    last_text_had_nearby_image   = False

    for blk in blocks:
        kind = blk.get("kind")

        if kind == "image":
            img_idx += 1
            ext   = blk.get("ext", "png")
            fname = f"{img_prefix}_{img_idx:03d}.{ext}"
            images[fname] = blk["data"]

            if last_text_had_nearby_image:
                html.append(
                    f'<figure class="float-image">'
                    f'<img src="images/{fname}" alt=""/>'
                    f'</figure>'
                )
                last_text_had_nearby_image = False
            else:
                html.append(
                    f'<div class="img-wrap">'
                    f'<img src="images/{fname}" alt=""/>'
                    f'</div>'
                )

        elif kind == "table":
            rows = blk.get("rows", [])

            if not rows:
                continue

            row_html: list[str] = []

            for ri, row in enumerate(rows):
                tag   = "th" if ri == 0 else "td"
                cells = "".join(
                    f"<{tag}>{_sanitize(cell)}</{tag}>" for cell in row
                )
                row_html.append(f"<tr>{cells}</tr>")

            html.append(f'<table>{"".join(row_html)}</table>')
            last_text_had_nearby_image = False

        elif kind == "sidebar":
            rendered = _render_sidebar_block(blk)

            if rendered:
                html.append(rendered)

            last_text_had_nearby_image = blk.get("nearby_image", False)

        elif kind == "text":
            lines = blk.get("lines", [])

            if not lines:
                continue

            block_html: list[str] = []
            _render_text_lines(lines, block_html)
            html.extend(block_html)
            last_text_had_nearby_image = blk.get("nearby_image", False)

    return "\n".join(html)


# ── Plain-text rendering ──────────────────────────────────────────────────────


def smart_join_paragraphs(text: str) -> list[str]:
    lines         = [l.rstrip() for l in text.split("\n")]
    content_lines = [l for l in lines if l.strip()]
    blank_lines   = [l for l in lines if not l.strip()]

    dense_blank_mode = bool(
        content_lines and len(blank_lines) > len(content_lines) * 0.4
    )

    paras: list[str]        = []
    current_words: list[str] = []
    prev_ended_sentence      = False

    for line in lines:
        stripped = line.strip()

        if not stripped:
            if not dense_blank_mode and current_words:
                paras.append(" ".join(current_words))
                current_words = []
            continue

        words_in_line       = stripped.split()
        is_short_standalone = len(words_in_line) <= 4 and (
            not current_words or prev_ended_sentence
        )

        if dense_blank_mode:
            if current_words and (
                (prev_ended_sentence and stripped[0].isupper())
                or is_short_standalone
            ):
                paras.append(" ".join(current_words))
                current_words = []

            current_words.append(stripped)
            clean_end           = stripped.rstrip(' "\'»)')
            prev_ended_sentence = bool(clean_end and clean_end[-1] in ".!?:")
        else:
            current_words.append(stripped)

    if current_words:
        paras.append(" ".join(current_words))

    return paras if paras else [text.strip()]


def _render_text_chapter(chap_content: str) -> str:
    safe_content = _sanitize(chap_content)
    para_blocks  = smart_join_paragraphs(safe_content)

    return "\n".join(
        f"<p>{block}</p>" for block in para_blocks if block.strip()
    )


# ── Cover extraction ──────────────────────────────────────────────────────────


def extract_cover_from_docx(docx_path: str) -> bytes | None:
    """
    Render the first page of a .docx file to a PNG for use as an EPUB cover.

    Strategy (tried in order):
      1. LibreOffice headless → PDF → PyMuPDF → PNG
      2. Return None gracefully (EPUB will be generated without a cover)

    The intermediate PDF is written to a temp directory and cleaned up after.
    """
    import tempfile
    import shutil
    import logging

    log = logging.getLogger(__name__)

    tmp = tempfile.mkdtemp(prefix="docx_cover_")

    try:
        # Step 1: docx → pdf via LibreOffice
        from processor import _find_soffice
        import subprocess

        soffice = _find_soffice()

        result = subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf",
             "--outdir", tmp, docx_path],
            capture_output=True,
            timeout=120,
        )

        if result.returncode != 0:
            log.info(
                f"LibreOffice cover render failed (exit {result.returncode}), "
                "generating EPUB without a cover image."
            )
            return None

        basename = os.path.splitext(os.path.basename(docx_path))[0]
        pdf_tmp  = os.path.join(tmp, f"{basename}.pdf")

        if not os.path.exists(pdf_tmp):
            log.info("LibreOffice produced no PDF for cover, skipping.")
            return None

        # Step 2: first page → PNG via PyMuPDF (already a dependency)
        return extract_cover_image(pdf_tmp)

    except Exception as e:
        log.info(f"DOCX cover extraction skipped: {e}")
        return None

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── EPUB assembly ─────────────────────────────────────────────────────────────


def build_epub(
    text: str,
    title: str,
    author: str,
    out_dir: str,
    pdf_path:  str | None = None,
    docx_path: str | None = None,          # ← NEW: pass for Word-sourced EPUBs
    rich_chapters: list[tuple[str, list[dict]]] | None = None,
) -> str:
    """
    Assemble an EPUB file from structured content.

    Parameters
    ----------
    text          : Plain-text fallback content (used when rich_chapters is None).
    title         : Book title (used in metadata and filename).
    author        : Author name.
    out_dir       : Directory where the .epub will be written.
    pdf_path      : Source PDF path (used for cover image extraction).
    docx_path     : Source DOCX/DOC path (used for cover image when pdf_path
                    is not provided).  LibreOffice must be installed for cover
                    extraction from Word files.
    rich_chapters : Pre-extracted rich chapters (list of (title, blocks)).
                    When supplied, overrides plain-text chapter detection.

    Returns
    -------
    Absolute path to the generated .epub file.
    """
    safe_title = re.sub(r"[^\w\s-]", "", title).strip()
    safe_title = re.sub(r"\s+", "_", safe_title)
    output     = os.path.join(out_dir, f"{safe_title}.epub")

    use_rich = rich_chapters is not None and len(rich_chapters) > 0

    if use_rich:
        raw_chapters = rich_chapters
    else:
        raw_chapters = extract_chapters_from_text(text) or [
            ("Content", text or "No content extracted.")
        ]

    # ── Cover image ────────────────────────────────────────────────────────
    cover_png: bytes | None = None

    if pdf_path:
        cover_png = extract_cover_image(pdf_path)

    if cover_png is None and docx_path:
        cover_png = extract_cover_from_docx(docx_path)

    # ── Chapter rendering ──────────────────────────────────────────────────
    images: dict[str, bytes]                       = {}
    chapter_files: list[tuple[str, str, str]]      = []

    for chap_title, chap_data in raw_chapters:
        if chap_title.strip().lower() in SKIP_CHAPTERS:
            continue

        safe_chap_title = _sanitize(chap_title)
        chap_num        = len(chapter_files) + 1
        fname           = f"chap_{chap_num:02d}.xhtml"

        if use_rich:
            img_prefix = f"chap{chap_num:02d}"
            body_html  = _render_rich_blocks(chap_data, images, img_prefix)
        else:
            body_html  = _render_text_chapter(chap_data)

        xhtml = _chapter_xhtml(safe_chap_title, body_html)
        chapter_files.append((fname, safe_chap_title, xhtml))

    # ── OPF manifests ──────────────────────────────────────────────────────
    image_manifest = "\n    ".join(
        f'<item id="img-{fn.replace(".", "-")}" href="images/{fn}" '
        f'media-type="image/{_img_media_type(fn)}"/>'
        for fn in images
    )

    chapter_manifest = "\n    ".join(
        f'<item id="chap{i+1}" href="{fname}" media-type="application/xhtml+xml"/>'
        for i, (fname, _, __) in enumerate(chapter_files)
    )

    spine_items = "\n    ".join(
        f'<itemref idref="chap{i+1}"/>' for i in range(len(chapter_files))
    )

    toc_nav_points = "\n    ".join(
        f'<navPoint id="np{i+1}" playOrder="{i+1}">'
        f'<navLabel><text>{ct}</text></navLabel>'
        f'<content src="{fn}"/></navPoint>'
        for i, (fn, ct, _) in enumerate(chapter_files)
    )

    toc_links = "\n".join(
        f'<li><a href="{fn}">{ct}</a></li>' for fn, ct, _ in chapter_files
    )

    cover_manifest = ""
    cover_meta     = ""

    if cover_png:
        cover_manifest = (
            '<item id="cover-img" href="cover.png" media-type="image/png"/>\n    '
            '<item id="cover-page" href="cover.xhtml" media-type="application/xhtml+xml"/>'
        )
        cover_meta = '<meta name="cover" content="cover-img"/>'

    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{_sanitize(title)}</dc:title>
    <dc:creator>{_sanitize(author)}</dc:creator>
    <dc:language>en</dc:language>
    <dc:identifier id="bookid">id-{safe_title}</dc:identifier>
    {cover_meta}
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="toc" href="toc.xhtml" media-type="application/xhtml+xml"/>
    {cover_manifest}
    {chapter_manifest}
    {image_manifest}
  </manifest>
  <spine toc="ncx">
    {'<itemref idref="cover-page"/>' if cover_png else ''}
    <itemref idref="toc"/>
    {spine_items}
  </spine>
</package>"""

    ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="id-{safe_title}"/></head>
  <docTitle><text>{_sanitize(title)}</text></docTitle>
  <navMap>{toc_nav_points}</navMap>
</ncx>"""

    toc_xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
<title>Table of Contents</title>
<style>
  body {{ font-family: Arial, sans-serif; margin: 2em 1em; }}
  h1   {{ font-size: 1.4em; font-weight: bold; margin-bottom: 1em; text-align: center; }}
  ul   {{ list-style: none; padding: 0; }}
  li   {{ margin: 0.5em 0; padding: 0.4em 0; border-bottom: 1px solid #ddd; }}
  a    {{ text-decoration: none; color: #000; font-size: 1.05em; }}
</style>
</head>
<body>
  <h1>Table of Contents</h1>
  <ul>{toc_links}</ul>
</body>
</html>"""

    cover_xhtml = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
<title>Cover</title>
<style>
body {
    margin: 0;
    padding: 0;
    text-align: center;
    background: #ffffff;
}

img {
    display: block;
    margin: 0 auto;
    max-width: 100%;
    max-height: 100vh;
    height: auto;
}
</style>
</head>
<body>
    <img src="cover.png" alt="Cover"/>
</body>
</html>"""

    # ── Write EPUB zip ─────────────────────────────────────────────────────
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "mimetype",
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )

        zf.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf"
              media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>""",
        )

        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/toc.ncx", ncx)
        zf.writestr("OEBPS/toc.xhtml", toc_xhtml)

        if cover_png:
            zf.writestr("OEBPS/cover.png", cover_png)
            zf.writestr("OEBPS/cover.xhtml", cover_xhtml)

        for img_fname, img_bytes in images.items():
            zf.writestr(f"OEBPS/images/{img_fname}", img_bytes)

        for fname, _, xhtml in chapter_files:
            zf.writestr(f"OEBPS/{fname}", xhtml)

    return output


def _img_media_type(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower()

    return {
        "jpg":  "jpeg",
        "jpeg": "jpeg",
        "png":  "png",
        "gif":  "gif",
        "webp": "webp",
        "svg":  "svg+xml",
    }.get(ext, "png")
