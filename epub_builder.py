import os
import re
import zipfile
import base64

from processor import extract_chapters_from_text, extract_cover_image

SKIP_CHAPTERS = {"contents", "table of contents", "content"}


def smart_join_paragraphs(text: str) -> list[str]:
    """
    Join PDF lines into real paragraphs.
    Handles both blank-line-delimited and dense (no blanks) PDF exports.
    """
    lines = [l.rstrip() for l in text.split("\n")]
    content_lines = [l for l in lines if l.strip()]
    blank_lines = [l for l in lines if not l.strip()]

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


def _sanitize(text: str) -> str:
    """Strip XML control chars and escape XML special chars."""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_epub(text: str, title: str, author: str, out_dir: str, pdf_path: str | None = None) -> str:
    safe_title = re.sub(r"[^\w\s-]", "", title).strip()
    safe_title = re.sub(r"\s+", "_", safe_title)
    output = os.path.join(out_dir, f"{safe_title}.epub")

    chapters = extract_chapters_from_text(text) or [("Content", text or "No content extracted.")]

    # Try to extract a cover image from the original PDF
    cover_png: bytes | None = None
    if pdf_path:
        cover_png = extract_cover_image(pdf_path)

    chapter_files: list[tuple[str, str, str]] = []
    for chap_title, chap_content in chapters:
        if chap_title.strip().lower() in SKIP_CHAPTERS:
            continue

        safe_content = _sanitize(chap_content)
        safe_chap_title = _sanitize(chap_title)

        para_blocks = smart_join_paragraphs(safe_content)
        paragraphs = "\n".join(
            f"<p>{block}</p>" for block in para_blocks if block.strip()
        )

        xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <title>{safe_chap_title}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 0pt 14pt; line-height: 120%; color: #000; font-size: 1.09em; }}
    h1 {{ font-size: 1.4em; font-weight: bold; margin: 28pt 0pt; text-align: center; line-height: 120%; }}
    p {{ margin: 0pt 0pt 8.5pt; text-indent: 14pt; text-align: justify; line-height: 120%; widows: 0; orphans: 0; }}
  </style>
</head>
<body>
  <h1>{safe_chap_title}</h1>
  {paragraphs}
</body>
</html>"""
        chapter_files.append((f"chap_{len(chapter_files)+1:02d}.xhtml", safe_chap_title, xhtml))

    manifest_items = "\n    ".join(
        f'<item id="chap{i+1}" href="{fname}" media-type="application/xhtml+xml"/>'
        for i, (fname, _, __) in enumerate(chapter_files)
    )
    spine_items = "\n    ".join(
        f'<itemref idref="chap{i+1}"/>' for i in range(len(chapter_files))
    )
    toc_nav_points = "\n    ".join(
        f'<navPoint id="np{i+1}" playOrder="{i+1}"><navLabel><text>{ct}</text></navLabel><content src="{fn}"/></navPoint>'
        for i, (fn, ct, _) in enumerate(chapter_files)
    )
    toc_links = "\n".join(
        f'<li><a href="{fn}">{ct}</a></li>' for fn, ct, _ in chapter_files
    )

    # Optional cover manifest entry
    cover_manifest = ""
    cover_meta = ""
    if cover_png:
        cover_manifest = '<item id="cover-img" href="cover.png" media-type="image/png"/>\n    <item id="cover-page" href="cover.xhtml" media-type="application/xhtml+xml"/>'
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
    {manifest_items}
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
  h1 {{ font-size: 1.4em; font-weight: bold; margin-bottom: 1em; text-align: center; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ margin: 0.5em 0; padding: 0.4em 0; border-bottom: 1px solid #ddd; }}
  a {{ text-decoration: none; color: #000; font-size: 1.05em; }}
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
<head><title>Cover</title>
<style>body{{margin:0;padding:0;text-align:center;}} img{{max-width:100%;max-height:100%;display:block;margin:0 auto;}}</style>
</head>
<body><img src="cover.png" alt="Cover"/></body>
</html>"""

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>""")
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/toc.ncx", ncx)
        zf.writestr("OEBPS/toc.xhtml", toc_xhtml)

        if cover_png:
            zf.writestr("OEBPS/cover.png", cover_png)
            zf.writestr("OEBPS/cover.xhtml", cover_xhtml)

        for fname, _, xhtml in chapter_files:
            zf.writestr(f"OEBPS/{fname}", xhtml)

    return output
