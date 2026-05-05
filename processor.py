from pdfminer.high_level import extract_text
from pdf2image import convert_from_path
import pytesseract
import re


def extract_text_from_pdf(pdf_path: str) -> str:
    try:
        text = extract_text(pdf_path)
        return text if text else ""
    except:
        return ""


def ocr_pdf_if_needed(pdf_path: str) -> str:
    images = convert_from_path(pdf_path)
    text = []
    for img in images:
        text.append(pytesseract.image_to_string(img))
    return "\n".join(text)


def extract_chapters_from_text(text: str):
    """
    Detects chapters using multiple strategies.
    Returns list of (title, content) tuples.
    """
    lines = text.split('\n')
    chapters = []
    current_title = None
    current_content = []

    # Strategy 1: numbered chapter pattern
    # Matches: "Chapter 1", "CHAPTER ONE", "1.", "I.", standalone numbers followed by title
    chapter_patterns = [
        re.compile(r'^chapter\s+\d+[\s:\-–—]*(.+)?$', re.IGNORECASE),
        re.compile(r'^chapter\s+[ivxlcdmIVXLCDM]+[\s:\-–—]*(.+)?$', re.IGNORECASE),
        re.compile(r'^\d+\.\s+[A-Z].+$'),
        re.compile(r'^[IVXLCDM]+\.\s+[A-Z].+$'),
    ]

    # Strategy 2: ALL CAPS lines that look like titles (5+ words or meaningful)
    section_pattern = re.compile(r'^[A-Z][A-Z\s\-]{8,}$')

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if not line:
            current_content.append('')
            i += 1
            continue

        is_chapter = any(p.match(line) for p in chapter_patterns)
        is_section = section_pattern.match(line) and len(line.split()) >= 3

        if is_chapter or is_section:
            # Save previous chapter
            if current_title and any(c.strip() for c in current_content):
                chapters.append((current_title, '\n'.join(current_content).strip()))
            current_title = line
            current_content = []
        else:
            if current_title is None:
                current_title = "Front Matter"
            current_content.append(line)

        i += 1

    # Save last chapter
    if current_title and any(c.strip() for c in current_content):
        chapters.append((current_title, '\n'.join(current_content).strip()))

    # Fallback: if still no good chapters, split by word count
    if len(chapters) <= 2:
        return split_by_wordcount(text)

    return chapters


def split_by_wordcount(text: str, words_per_part: int = 800):
    """Fallback: split text into parts of roughly equal word count."""
    words = text.split()
    parts = []
    part_num = 1
    for i in range(0, len(words), words_per_part):
        chunk = ' '.join(words[i:i + words_per_part])
        parts.append((f"Part {part_num}", chunk))
        part_num += 1
    return parts if parts else [("Content", text)]
