"""
Small base64-encoded icon assets for use inside custom CSS (as a button's
background-image - see the <style> block built in app.py's Add Word tab).
Resized down from the real images/vocapp_book_only.png logo so book-icon
buttons show the actual brand mark, not a generic emoji glyph.
"""

import base64
import io
from pathlib import Path

from PIL import Image

IMAGES_DIR = Path(__file__).parent / "images"


def _data_uri(path: Path, size: int) -> str:
    im = Image.open(path).convert("RGBA")
    im.thumbnail((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


# Computed once at import time (module-level constant) - cheap enough
# (one small resize) that there's no need to cache it further.
BOOK_ICON_DATA_URI = _data_uri(IMAGES_DIR / "vocapp_book_only.png", 48)
