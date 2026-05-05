from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
import tempfile
import os

from processor import extract_text_from_pdf, ocr_pdf_if_needed
from epub_builder import build_epub

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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
        pdf_path = os.path.join(tmpdir, "input.pdf")

        with open(pdf_path, "wb") as f:
            f.write(await pdf.read())

        try:
            text = extract_text_from_pdf(pdf_path)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Text extraction failed: {str(e)}")

        if not text or len(text.strip()) < 50:
            try:
                text = ocr_pdf_if_needed(pdf_path)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"OCR failed: {str(e)}")

        try:
            epub_path = build_epub(text, title, author, tmpdir)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"EPUB build failed: {str(e)}")

        if not os.path.exists(epub_path):
            raise HTTPException(status_code=500, detail="EPUB file was not created")

        # Read file into memory BEFORE temp directory is deleted
        with open(epub_path, "rb") as f:
            epub_bytes = f.read()

    safe_title = title.replace(" ", "_")
    return Response(
        content=epub_bytes,
        media_type="application/epub+zip",
        headers={"Content-Disposition": f"attachment; filename={safe_title}.epub"}
    )
