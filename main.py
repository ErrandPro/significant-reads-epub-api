from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse
import tempfile
import os

from processor import extract_text_from_pdf, ocr_pdf_if_needed
from epub_builder import build_epub

app = FastAPI()
@app.get("/health")
def health():
    return {"status": "ok"}
@app.post("/convert")
async def convert_pdf(
    pdf: UploadFile = File(...),
    title: str = Form(...),
    author: str = Form(...)
):

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, pdf.filename)

        with open(pdf_path, "wb") as f:
            f.write(await pdf.read())

        text = extract_text_from_pdf(pdf_path)

        if not text or len(text.strip()) < 50:
            text = ocr_pdf_if_needed(pdf_path)

        epub_path = build_epub(text, title, author, tmpdir)

        return FileResponse(
            epub_path,
            media_type="application/epub+zip",
            filename=f"{title}.epub"
        )
