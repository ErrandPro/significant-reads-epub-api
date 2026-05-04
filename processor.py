from pdfminer.high_level import extract_text
from pdf2image import convert_from_path
import pytesseract

def extract_text_from_pdf(pdf_path: str):
    try:
        return extract_text(pdf_path)
    except:
        return ""

def ocr_pdf_if_needed(pdf_path: str):
    images = convert_from_path(pdf_path)
    text = []

    for img in images:
        text.append(pytesseract.image_to_string(img))

    return "\n".join(text)
