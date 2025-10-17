import fitz, sys
from pathlib import Path

path = Path(input("Enter path to PDF: ").strip())
try:
    doc = fitz.open(str(path))
    print(f"Opened {path}, {doc.page_count} pages")
    page = doc.load_page(0)
    pix = page.get_pixmap()
    print(f"Rendered successfully: {pix.width}x{pix.height}")
except Exception as e:
    print("ERROR:", e)
