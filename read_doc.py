#!/usr/bin/env python3
"""
read_doc.py — Extract text from PDF, DOCX, TXT, and image files.
Usage: python3 read_doc.py <file_path> [pages|lines|all]

Dependencies: pypdf (PDF), python-docx (DOCX), Pillow (images)
Install: pip install pypdf python-docx Pillow --break-system-packages
"""

import sys
import os

def read_pdf(path):
    import pypdf
    reader = pypdf.PdfReader(path)
    pages = len(reader.pages)
    text_parts = []
    for i, page in enumerate(reader.pages):
        t = page.extract_text() or ""
        text_parts.append(f"[Page {i+1}/{pages}]" if t.strip() else f"[Page {i+1}/{pages}] (empty)")
        if t.strip():
            text_parts.append(t)
    return "\n".join(text_parts)

def read_docx(path):
    from docx import Document
    doc = Document(path)
    parts = []
    for i, para in enumerate(doc.paragraphs):
        if para.text.strip():
            parts.append(para.text)
    return "\n".join(parts)

def read_txt(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()

def read_image(path):
    from PIL import Image
    # For now just return dimensions + note that vision tool should be used
    img = Image.open(path)
    return f"[Image: {img.format}, {img.size[0]}x{img.size[1]} pixels]\nNote: Use vision_analyze tool for AI-powered content extraction."

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 read_doc.py <file_path>")
        sys.exit(1)

    path = sys.argv[1]
    if not os.path.exists(path):
        print(f"File not found: {path}")
        sys.exit(1)

    ext = os.path.splitext(path)[1].lower()
    print(f"=== {os.path.basename(path)} ===")

    try:
        if ext == ".pdf":
            print(read_pdf(path))
        elif ext in [".docx", ".doc"]:
            print(read_docx(path))
        elif ext in [".txt", ".md", ".csv", ".json"]:
            print(read_txt(path))
        elif ext in [".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"]:
            print(read_image(path))
        else:
            print(f"Unsupported file type: {ext}")
            print("Supported: .pdf, .docx, .doc, .txt, .md, .csv, .json, .png, .jpg, .webp, .gif, .bmp")
    except Exception as e:
        print(f"Error reading file: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
