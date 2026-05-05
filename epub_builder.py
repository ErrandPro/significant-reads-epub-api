import os
import re
import zipfile

def build_epub(text, title, author, out_dir):

    safe_title = re.sub(r'[^\w\s-]', '', title).strip()
    safe_title = re.sub(r'\s+', '_', safe_title)
    output = os.path.join(out_dir, f"{safe_title}.epub")

    chapters = [c.strip() for c in text.split("\n\n") if c.strip()]
    if not chapters:
        chapters = ["No content could be extracted from this PDF."]

    chapter_files = []
    for i, c in enumerate(chapters):
        safe_c = c.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        chapter_files.append((f"chap_{i+1}.xhtml", f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter {i+1}</title></head>
<body><h1>Chapter {i+1}</h1><p>{safe_c}</p></body>
</html>"""))

    manifest_items = '\n'.join(
        f'<item id="chap{i+1}" href="{fname}" media-type="application/xhtml+xml"/>'
        for i, (fname, _) in enumerate(chapter_files)
    )
    spine_items = '\n'.join(
        f'<itemref idref="chap{i+1}"/>'
        for i in range(len(chapter_files))
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
    {manifest_items}
  </manifest>
  <spine toc="ncx">
    {spine_items}
  </spine>
</package>"""

    ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="id-{safe_title}"/></head>
  <docTitle><text>{title}</text></docTitle>
  <navMap>
    {''.join(f'<navPoint id="np{i+1}" playOrder="{i+1}"><navLabel><text>Chapter {i+1}</text></navLabel><content src="{fname}"/></navPoint>' for i, (fname, _) in enumerate(chapter_files))}
  </navMap>
</ncx>"""

    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>""")
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/toc.ncx", ncx)
        for fname, content in chapter_files:
            zf.writestr(f"OEBPS/{fname}", content)

    return output
