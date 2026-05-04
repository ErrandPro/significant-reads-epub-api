from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import fitz  # PyMuPDF
import pdfplumber
from ebooklib import epub
import io

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/convert")
async def convert(
    pdf: UploadFile = File(...),
    title: str = Form(...),
    author: str = Form(...),
):
    pdf_bytes = await pdf.read()

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    book = epub.EpubBook()
    book.set_title(title)
    book.add_author(author)

    chapters = []

    for i, page in enumerate(doc):
        text = page.get_text()

        chapter = epub.EpubHtml(
            title=f"Page {i+1}",
            file_name=f"chap_{i+1}.xhtml",
            lang="en"
        )

        chapter.content = f"<h2>Page {i+1}</h2><p>{text}</p>"
        book.add_item(chapter)
        chapters.append(chapter)

    book.spine = ["nav"] + chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    output = io.BytesIO()
    epub.write_epub(output, book)

    output.seek(0)

    return StreamingResponse(
        output,
        media_type="application/epub+zip",
        headers={"Content-Disposition": f"attachment; filename={title}.epub"}
    )
