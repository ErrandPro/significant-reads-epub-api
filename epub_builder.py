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

    /* ── Sidebar (Phase 2) ──────────────────────────────────────────────
       Renders as a self-contained box that sits outside the main paragraph
       flow.  EPUB renderers vary in float support so we avoid float and
       instead use a full-width block with distinct visual treatment.
       The box is deliberately kept simple for maximum renderer compat:
       left border accent + light background + slightly smaller font.     */
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
        text-indent: 0;          /* sidebars rarely use indent */
        text-align: left;
    }
    .sidebar h2 {
        font-size: 1.05em;
        margin: 0pt 0pt 4pt;
        color: #2a5f85;
    }

    /* ── Image float (Phase 3) ──────────────────────────────────────────
       Used when an image was detected as horizontally adjacent to body
       text in the source PDF (float-style layout).  We can't replicate
       true CSS float reliably across EPUB renderers, so we emit a centred
       figure that is visually distinct from a plain img-wrap but still
       renders safely on all readers.  The descriptive caption slot is
       reserved via the figcaption rule even if we don't populate it yet. */
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

    /* ── Drop cap (Phase 4) ─────────────────────────────────────────────
       A drop cap is a large decorative first letter at the start of a
       chapter or section.  The PDF encodes it as an oversized standalone
       glyph; we re-emit it as a dropcap span merged into the opening
       paragraph so it flows with the sentence.
       Float is used here because it is the one place in the EPUB CSS
       where it is genuinely safe: a single character inline within a
       paragraph that degrades gracefully on renderers that ignore float.  */
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


# ── Rich rendering (images + tables + formatted text + sidebars) ──────────────

def _render_spans(spans: list[dict]) -> str:
    """Convert a list of span dicts to inline HTML with bold/italic tags."""
    parts: list[str] = []
    for s in spans:
        text = _sanitize(s.get("text", ""))
        if not text.strip():
            parts.append(text)
            continue
        b, i = s.get("bold", False), s.get("italic", False)
        if b and i:
            text = f"<strong><em>{text}</em></strong>"
        elif b:
            text = f"<strong>{text}</strong>"
        elif i:
            text = f"<em>{text}</em>"
        parts.append(text)
    return "".join(parts)


def _render_text_lines(lines: list[dict], html: list[str]) -> None:
    """
    Render a list of line-dicts (from a text or sidebar block) into `html`.

    Lines marked is_section become <h2>; all other lines are joined into
    a single <p> per contiguous non-heading run.

    Phase 4: the first body line in a block may carry a "dropcap_char" key.
    When present, a <span class="dropcap"> is prepended to that line's <p>
    and the paragraph's text-indent is suppressed inline so the drop cap
    sits flush with the left margin.

    This helper is shared between the "text" and "sidebar" block renderers
    so the line/span logic lives in exactly one place.
    """
    para_parts: list[str] = []
    # Phase 4: carry the dropcap from the first line into the first <p>
    pending_dropcap_html: str | None = None

    for ln in lines:
        spans      = ln.get("spans", [])
        is_section = ln.get("is_section", False)
        rendered   = _render_spans(spans).strip()
        if not rendered:
            continue

        # Phase 4: pick up dropcap_char from the line dict
        dropcap_char = ln.get("dropcap_char")
        if dropcap_char and pending_dropcap_html is None:
            safe_dc = _sanitize(dropcap_char)
            pending_dropcap_html = f'<span class="dropcap">{safe_dc}</span>'

        if is_section:
            if para_parts:
                p_content = "".join(para_parts).strip()
                if pending_dropcap_html:
                    # Flush dropcap with this paragraph; suppress indent
                    html.append(
                        f'<p style="text-indent:0">'
                        f'{pending_dropcap_html}{p_content}</p>'
                    )
                    pending_dropcap_html = None
                else:
                    html.append(f'<p>{p_content}</p>')
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
            html.append(f'<p>{p_content}</p>')


def _render_sidebar_block(blk: dict) -> str:
    """
    Render a sidebar block as a <div class="sidebar"> containing the same
    paragraph/heading structure as a normal text block.
    """
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
    """
    Render a list of rich blocks to an XHTML body string.
    Image bytes are collected into `images` (keyed by filename).

    Handles four block kinds:
      "text"    → <p> / <h2> paragraphs  (Phase 4: first <p> may have dropcap)
      "sidebar" → <div class="sidebar"> with inner <p> / <h2>
      "image"   → <div class="img-wrap"><img .../></div>  OR
                  <figure class="float-image"><img .../></figure>
                  (Phase 3: figure used when preceding text block had nearby_image)
      "table"   → <table> with <th> header row and <td> data rows

    Phase 3 logic:
      We track whether the most-recently-rendered text/sidebar block had the
      nearby_image flag.  When the next block is an image and that flag is set,
      the image is wrapped in <figure class="float-image"> instead of the plain
      <div class="img-wrap">.  The flag is consumed once used so it doesn't
      bleed onto subsequent images.
    """
    html: list[str] = []
    img_idx = 0
    # Phase 3: set to True after a text block with nearby_image=True;
    # consumed (reset to False) at the next image block.
    last_text_had_nearby_image: bool = False

    for blk in blocks:
        kind = blk.get("kind")

        # ── Image ────────────────────────────────────────────────────────
        if kind == "image":
            img_idx += 1
            ext   = blk.get("ext", "png")
            fname = f"{img_prefix}_{img_idx:03d}.{ext}"
            images[fname] = blk["data"]

            # Phase 3: choose wrapper based on whether preceding text
            # block was annotated as adjacent to this image.
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

        # ── Table ────────────────────────────────────────────────────────
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

        # ── Sidebar (Phase 2) ────────────────────────────────────────────
        elif kind == "sidebar":
            rendered = _render_sidebar_block(blk)
            if rendered:
                html.append(rendered)
            # Phase 3: sidebars can also carry the nearby_image flag
            last_text_had_nearby_image = blk.get("nearby_image", False)

        # ── Text (normal body paragraph) ─────────────────────────────────
        elif kind == "text":
            lines = blk.get("lines", [])
            if not lines:
                continue
            block_html: list[str] = []
            _render_text_lines(lines, block_html)
            html.extend(block_html)
            # Phase 3: propagate nearby_image flag to the next image block
            last_text_had_nearby_image = blk.get("nearby_image", False)

    return "\n".join(html)


# ── Plain-text rendering (fallback) ──────────────────────────────────────────

def smart_join_paragraphs(text: str) -> list[str]:
    """
    Join PDF lines into real paragraphs.
    Handles both blank-line-delimited and dense (no blanks) PDF exports.
    """
    lines = [l.rstrip() for l in text.split("\n")]
    content_lines = [l for l in lines if l.strip()]
    blank_lines   = [l for l in lines if not l.strip()]

    dense_blank_mode = bool(
        content_lines and len(blank_lines) > len(content_lines) * 0.4
    )

    paras: list[str] = []
    current_words: list[str] = []
    prev_ended_sentence = False

    for line in lines:
        stripped = line.strip()

        if not stripped:
            if not dense_blank_mode and current_words:
                paras.append(" ".join(current_words))
                current_words = []
            continue

        words_in_line = stripped.split()
        is_short_standalone = len(words_in_line) <= 4 and (
            not current_words or prev_ended_sentence
        )

        if dense_blank_mode:
            if current_words and (
                (prev_ended_sentence and stripped[0].isupper()) or is_short_standalone
            ):
                paras.append(" ".join(current_words))
                current_words = []
            current_words.append(stripped)
            clean_end = stripped.rstrip(' "\'»)')
            prev_ended_sentence = bool(clean_end and clean_end[-1] in ".!?:")
        else:
            current_words.append(stripped)

    if current_words:
        paras.append(" ".join(current_words))

    return paras if paras else [text.strip()]


def _render_text_chapter(chap_content: str) -> str:
    """Render plain-text chapter content to XHTML paragraph blocks."""
    safe_content = _sanitize(chap_content)
    para_blocks  = smart_join_paragraphs(safe_content)
    return "\n".join(
        f"<p>{block}</p>" for block in para_blocks if block.strip()
    )


# ── EPUB assembly ─────────────────────────────────────────────────────────────

def build_epub(
    text: str,
    title: str,
    author: str,
    out_dir: str,
    pdf_path: str | None = None,
    rich_chapters: list[tuple[str, list[dict]]] | None = None,
) -> str:
    """
    Build an EPUB from either:
      • rich_chapters (images + tables + formatting + sidebars) — preferred, or
      • text           (plain text fallback)

    The pdf_path is used solely to extract a cover image.
    """
    safe_title = re.sub(r"[^\w\s-]", "", title).strip()
    safe_title = re.sub(r"\s+", "_", safe_title)
    output = os.path.join(out_dir, f"{safe_title}.epub")

    use_rich = rich_chapters is not None and len(rich_chapters) > 0

    if use_rich:
        raw_chapters = rich_chapters                          # type: ignore[assignment]
    else:
        raw_chapters = extract_chapters_from_text(text) or [("Content", text or "No content extracted.")]

    cover_png: bytes | None = None
    if pdf_path:
        cover_png = extract_cover_image(pdf_path)

    images: dict[str, bytes] = {}
    chapter_files: list[tuple[str, str, str]] = []

    for chap_title, chap_data in raw_chapters:
        if chap_title.strip().lower() in SKIP_CHAPTERS:
            continue

        safe_chap_title = _sanitize(chap_title)
        chap_num = len(chapter_files) + 1
        fname    = f"chap_{chap_num:02d}.xhtml"

        if use_rich:
            img_prefix = f"chap{chap_num:02d}"
            body_html  = _render_rich_blocks(chap_data, images, img_prefix)  # type: ignore[arg-type]
        else:
            body_html  = _render_text_chapter(chap_data)                     # type: ignore[arg-type]

        xhtml = _chapter_xhtml(safe_chap_title, body_html)
        chapter_files.append((fname, safe_chap_title, xhtml))

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
<head><title>Table of Contents</title>
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

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "mimetype", "application/epub+zip",
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
