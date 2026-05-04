from ebooklib import epub
import os

def build_epub(text, title, author, out_dir):

    book = epub.EpubBook()

    book.set_title(title)
    book.add_author(author)

    chapters = text.split("\n\n")
    items = []

    for i, c in enumerate(chapters):
        chap = epub.EpubHtml(
            title=f"Chapter {i+1}",
            file_name=f"chap_{i+1}.xhtml",
            content=f"<h1>Chapter {i+1}</h1><p>{c}</p>"
        )
        book.add_item(chap)
        items.append(chap)

    book.toc = tuple(items)
    book.spine = ["nav"] + items

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    output = os.path.join(out_dir, f"{title}.epub")
    epub.write_epub(output, book)

    return output
