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


def clean_line(line: str) -> str:
    """Remove page numbers and junk lines."""
    stripped = line.strip()
    # Remove standalone roman numerals (i, ii, iii ... xxv)
    if re.match(r'^[ivxlcdmIVXLCDM]{1,6}$', stripped):
        return ''
    # Remove standalone arabic page numbers
    if re.match(r'^\d{1,3}$', stripped):
        return ''
    return stripped


def is_main_chapter_heading(line: str) -> tuple:
    """
    Returns (True, title) if line is a MAIN chapter heading.
    Main chapters look like:
      - "Chapter 1" or "1" followed by a title on next line
      - Named sections: Foreword, Introduction, Endorsements, Dedication, Acknowledgements
    """
    stripped = line.strip()

    # Named front/back matter sections
    named = ['foreword', 'introduction', 'endorsements', 'dedication',
             'acknowledgements', 'conclusion', 'preface', 'contents']
    if stripped.lower() in named:
        return True, stripped.title()

    # "Chapter N" pattern
    if re.match(r'^chapter\s+\d+', stripped, re.IGNORECASE):
        return True, stripped

    return False, None


def extract_chapters_from_text(text: str):
    """
    Extracts real chapters from the book text.
    Returns list of (title, content) tuples.
    """
    lines = text.split('\n')

    # Clean all lines first
    cleaned = []
    for line in lines:
        c = clean_line(line)
        cleaned.append(c)

    chapters = []
    current_title = "Front Matter"
    current_content = []
    i = 0

    while i < len(cleaned):
        line = cleaned[i]

        if not line:
            current_content.append('')
            i += 1
            continue

        is_heading, heading_title = is_main_chapter_heading(line)

        if is_heading:
            # Save previous chapter
            content_text = '\n'.join(current_content).strip()
            if content_text:
                chapters.append((current_title, content_text))
            current_title = heading_title
            current_content = []
            i += 1
            continue

        # Check for numbered chapter pattern:
        # A standalone digit on one line, followed by a title line
        # e.g. line="1", next line="My Salvation Journey"
        if re.match(r'^\d+$', line) and i + 1 < len(cleaned):
            next_line = cleaned[i + 1].strip()
            # Next line should look like a title (starts with capital, not too long)
            if next_line and next_line[0].isupper() and len(next_line) < 80 and not next_line.endswith('.'):
                # Check the line after that — if it's also a short title continuation
                title = next_line
                if i + 2 < len(cleaned):
                    after = cleaned[i + 2].strip()
                    if after and after[0].isupper() and len(after) < 60 and not after.endswith('.') and not re.match(r'^\d+$', after):
                        # Could be two-line title
                        title = next_line + ' ' + after
                        i += 1  # skip the extra title line

                content_text = '\n'.join(current_content).strip()
                if content_text:
                    chapters.append((current_title, content_text))
                current_title = title
                current_content = []
                i += 2
                continue

        current_content.append(line)
        i += 1

    # Save last chapter
    content_text = '\n'.join(current_content).strip()
    if content_text:
        chapters.append((current_title, content_text))

    # If we got very few chapters, fall back to word-count splitting
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
