from pdfminer.high_level import extract_text
from pdf2image import convert_from_path
import pytesseract
import re
from collections import Counter


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


def detect_running_headers(lines):
    """Find lines that repeat 3+ times — these are page headers/footers."""
    stripped = [l.strip() for l in lines if l.strip()]
    freq = Counter(stripped)
    return {line for line, count in freq.items() if count >= 3 and len(line) > 4}


def clean_text(text: str) -> str:
    """Remove control chars, running headers, page numbers, and fix concatenated headers."""
    # Strip invalid XML control characters
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)

    lines = text.split('\n')
    running_headers = detect_running_headers(lines)

    cleaned = []
    for line in lines:
        s = line.strip()

        # Skip standalone page numbers (arabic or roman)
        if re.match(r'^\d{1,3}$', s) or re.match(r'^[ivxlcdmIVXLCDM]{1,6}$', s):
            continue

        # Skip known running headers
        if s in running_headers:
            continue

        # Fix concatenated running headers: "Book TitleChapter content"
        # If a running header appears as a PREFIX on a line, strip it
        for header in running_headers:
            if s.startswith(header) and len(s) > len(header):
                remainder = s[len(header):].strip()
                line = remainder
                s = remainder
                break

        cleaned.append(line)

    return '\n'.join(cleaned)


def skip_toc(lines):
    """
    Detect and skip the Table of Contents section.
    TOC lines typically have dots (......) or trailing page numbers.
    Returns the index where real content begins.
    """
    toc_pattern = re.compile(r'\.{3,}|\s{2,}\d+\s*$')
    toc_hits = 0
    last_toc_line = 0

    for i, line in enumerate(lines[:150]):
        if toc_pattern.search(line.strip()):
            toc_hits += 1
            last_toc_line = i

    if toc_hits >= 3:
        # Skip past the TOC plus a few blank lines
        return last_toc_line + 3
    return 0


def is_chapter_heading(line):
    """Return True if this line looks like a main chapter heading."""
    s = line.strip()
    if not s or len(s) > 100:
        return False

    patterns = [
        re.compile(r'^chapter\s+\d+', re.IGNORECASE),
        re.compile(r'^(introduction|foreword|preface|dedication|acknowledgements?|endorsements?|conclusion|epilogue|prologue)$', re.IGNORECASE),
        # Numbered chapter like "1." or "Chapter One"
        re.compile(r'^chapter\s+(one|two|three|four|five|six|seven|eight|nine|ten)\b', re.IGNORECASE),
    ]
    return any(p.match(s) for p in patterns)


def is_section_heading(line):
    """All-caps lines under 80 chars are likely section/sub-chapter headings."""
    s = line.strip()
    if not s or len(s) > 80 or len(s) < 4:
        return False
    # Must be mostly uppercase letters and spaces/punctuation
    alpha = [c for c in s if c.isalpha()]
    if not alpha:
        return False
    upper_ratio = sum(1 for c in alpha if c.isupper()) / len(alpha)
    return upper_ratio > 0.85 and len(s.split()) >= 2


def extract_chapters_from_text(text: str):
    """Extract chapters from cleaned book text."""
    text = clean_text(text)
    lines = text.split('\n')

    # Skip table of contents
    start_idx = skip_toc(lines)
    lines = lines[start_idx:]

    chapters = []
    current_title = "Front Matter"
    current_content = []

    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()

        if not s:
            current_content.append('')
            i += 1
            continue

        if is_chapter_heading(s):
            # Save previous chapter
            content_text = '\n'.join(current_content).strip()
            if content_text:
                chapters.append((current_title, content_text))
            elif chapters:
                # Empty chapter — try to merge title with previous as subtitle
                pass

            # Look ahead for a subtitle on the very next non-empty line
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1

            subtitle = ''
            if j < len(lines):
                next_s = lines[j].strip()
                if (next_s and not is_chapter_heading(next_s) and
                        len(next_s) < 70 and len(next_s.split()) <= 7 and
                        next_s[0].isupper()):
                    subtitle = next_s
                    i = j + 1
                else:
                    i += 1
            else:
                i += 1

            current_title = f"{s}: {subtitle}" if subtitle else s
            current_content = []
            continue

        elif is_section_heading(s):
            # Section headings within a chapter — keep as content with marker
            current_content.append('')
            current_content.append(line)
            current_content.append('')
            i += 1
            continue

        current_content.append(line)
        i += 1

    # Save last chapter
    content_text = '\n'.join(current_content).strip()
    if content_text:
        chapters.append((current_title, content_text))

    # Quality check: if most chapters are tiny and one is huge, fallback
    if chapters:
        sizes = [len(c.split()) for _, c in chapters]
        max_size = max(sizes)
        tiny = sum(1 for s in sizes if s < 30)
        if tiny > len(chapters) * 0.5 and max_size > 2000:
            return split_by_wordcount(text)

    if not chapters:
        return split_by_wordcount(text)

    return chapters


def split_by_wordcount(text: str, words_per_part: int = 800):
    """Fallback: split text into roughly equal parts."""
    words = text.split()
    parts = []
    part_num = 1
    for i in range(0, len(words), words_per_part):
        chunk = ' '.join(words[i:i + words_per_part])
        parts.append((f"Part {part_num}", chunk))
        part_num += 1
    return parts if parts else [("Content", text)]
