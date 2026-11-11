"""Generate a blank, explicitly fictional PDF using the existing pypdf library."""

from io import BytesIO
from pathlib import Path
from pypdf import PdfWriter


def sample_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.add_metadata({"/Title": "FICTIONAL TEST RESUME - NO PERSONAL DATA"})
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


if __name__ == "__main__":
    path = Path(__file__).resolve().parents[2] / ".local/fixture-resume.pdf"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(sample_pdf_bytes())
    print(path)
