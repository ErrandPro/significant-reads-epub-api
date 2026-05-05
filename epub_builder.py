import os
import re
import zipfile
from processor import extract_chapters_from_text


def build_epub(text, title, author, out_dir):
    safe_title = re.sub(r'[^\w\s-]', '', title).strip()
    safe_title = re.sub(r'\s+', '_', safe_title)
    output = os.path.join(out_dir, f"{safe_title}.epub")

    chapters = extract_chapters_from_text(text)

    if not chapters:
        chapters = [("Content", text or "No content could be extracted.")]

    chapter_files = []
    for i, (chap_title, chap_content) in enumerate(chapters):
        safe_content = chap_content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        safe_chap_title = chap_title.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        paragraphs = '\n'.join(
            f'<p>{p.strip()}</p>' for p in safe_content.split('\n') if p.strip()
        )
        xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <title>{safe_chap_title}</title>
  <style>
    body {{ font-family: Georgia, serif; margin: 3em 2em; line-height: 1.8; color: #222; }}
    h1 {{ font-size: 1.4em; font-weight: bold; margin-bottom: 1.2em; border-bottom: 1px solid #ccc; padding-bottom: 0.4em; }}
    p {{ margin: 0.6em 0; text-indent: 1.5em; }}
  </style>
</head>
<body>
  <h1>{safe_chap_title}</h1>
  {paragraphs}
</body>
</html>"""
        chapter_files.append((f"chap_{i+1:02d}.xhtml", safe_chap_title, xhtml))

    manifest_items = '\n    '.join(
        f'<item id="chap{i+1}" href="{fname}" media-type="application/xhtml+xml"/>'
        for i, (fname, _, __) in enumerate(chapter_files)
    )
    spine_items = '\n    '.join(
        f'<itemref idref="chap{i+1}"/>'
        for i in range(len(chapter_files))
    )
    toc_nav_points = '\n    '.join(
        f'<navPoint id="np{i+1}" playOrder="{i+1}"><navLabel><text>{chap_title}</text></navLabel><content src="{fname}"/></navPoint>'
        for i, (fname, chap_title, _) in enumerate(chapter_files)
    )
    toc_links = '\n'.join(
        f'<li><a href="{fname}">{chap_title}</a></li>'
        for fname, chap_title, _ in chapter_files
    )

    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title>
    <dc:creator>{author}</dc:creator>
    <dc:language>en</dc:language>
    <dc:identifier id="bookid">id-{safe_title}</dc:identifier>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="toc" href="toc.xhtml" media-type="application/xhtml+xml"/>
    {manifest_items}
  </manifest>
  <spine toc="ncx">
    <itemref idref="toc"/>
    {spine_items}
  </spine>
</package>"""

    ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="id-{safe_title}"/></head>
  <docTitle><text>{title}</text></docTitle>
  <navMap>
    {toc_nav_points}
  </navMap>
</ncx>"""

    toc_xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Table of Contents</title>
<style>
  body {{ font-family: Georgia, serif; margin: 3em 2em; }}
  h1 {{ font-size: 1.5em; margin-bottom: 1em; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ margin: 0.6em 0; padding: 0.3em 0; border-bottom: 1px solid #eee; }}
  a {{ text-decoration: none; color: #333; font-size: 1em; }}
  a:hover {{ color: #000; }}
</style>
</head>
<body>
  <h1>Table of Contents</h1>
  <ul>
    {toc_links}
  </ul>
</body>
</html>"""

    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip",
                    compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>""")
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/toc.ncx", ncx)
        zf.writestr("OEBPS/toc.xhtml", toc_xhtml)
        for fname, _, xhtml in chapter_files:
            zf.writestr(f"OEBPS/{fname}", xhtml)

    return output
