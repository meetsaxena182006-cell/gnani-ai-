"""HTML -> PDF with Edge.  Usage: python tools/html_to_pdf.py in.html out.pdf"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

src, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", args=["--disable-gpu"])
    pg = b.new_page()
    pg.goto(src.as_uri(), wait_until="networkidle")
    pg.evaluate("document.fonts.ready")
    pg.wait_for_timeout(600)
    pg.pdf(path=str(out), format="A4", print_background=True, prefer_css_page_size=True)
    pg.set_viewport_size({"width": 794, "height": 1123})
    pg.screenshot(path=str(out.with_suffix(".png")), full_page=False)
    b.close()
print("ok", out, out.stat().st_size // 1024, "KB")
