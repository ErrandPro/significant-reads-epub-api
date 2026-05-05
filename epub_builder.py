from ebooklib import epub
import os
import re

def build_epub(text, title, author, out_dir):

    book = epub.EpubBook()
    book.set_identifier('id123456')
    book.set_title(title)
    book.set_language('en')
    book.add_author(author)

    # Split text into chapters
    chapters = [c.strip() for c in text.split("\n\n") if c.strip()]
    
    if not chapters:
        chapters = ["No content could be extracted from this PDF."]

    items = []

    for i, c in enumerate(chapters):
        chap = epub.EpubHtml(
            title=f"Chapter {i+1}",
            file_name=f"chap_{i+1}.xhtml",
            lang='en'
        )
        chap.content = f'<html><body><h1>Chapter {i+1}</h1><p>{c}</p></body></html>'
        book.add_item(chap)
        items.append(chap)

    book.toc = tuple(items)
    book.spine = ['nav'] + items
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # Sanitize filename
    safe_title = re.sub(r'[^\w\s-]', '', title).strip()
    output = os.path.join(out_dir, f"{safe_title}.epub")
    
    epub.write_epub(output, book)

    return output
