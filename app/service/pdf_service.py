from __future__ import annotations

import subprocess
import tempfile
from functools import lru_cache
from io import BytesIO
from xml.sax.saxutils import escape
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


class PDFRenderError(RuntimeError):
    pass


BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = BASE_DIR / "templates"
EXPORT_CSS_PATH = TEMPLATE_DIR / "memo_export.css"
EXPORT_TEMPLATE_NAME = "memo_export.html"
DEFAULT_BROWSER_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
]
RENDER_TIMEOUT_SECONDS = 90

JINJA_ENV = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)


def _browser_candidates() -> list[Path]:
    candidates = []
    for path in DEFAULT_BROWSER_CANDIDATES:
        if path.exists():
            candidates.append(path)
    return candidates


def _pick_browser() -> Path:
    candidates = _browser_candidates()
    if not candidates:
        raise PDFRenderError("No supported browser was found for PDF export. Install Chrome or Edge.")
    return candidates[0]


def _inline_css() -> str:
    if not EXPORT_CSS_PATH.exists():
        raise PDFRenderError(f"Missing PDF export stylesheet: {EXPORT_CSS_PATH}")
    return EXPORT_CSS_PATH.read_text(encoding="utf-8")


def _render_export_html(payload: dict) -> str:
    template = JINJA_ENV.get_template(EXPORT_TEMPLATE_NAME)
    context = dict(payload)
    context["inline_css"] = _inline_css()
    return template.render(context)


def _chrome_pdf_command(browser_path: Path, profile_dir: Path, html_path: Path, pdf_path: Path) -> list[str]:
    return [
        str(browser_path),
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-features=Translate,MediaRouter,OptimizationHints",
        "--disable-crash-reporter",
        "--allow-file-access-from-files",
        f"--user-data-dir={profile_dir}",
        "--print-to-pdf-no-header",
        f"--print-to-pdf={pdf_path}",
        html_path.as_uri(),
    ]


def _render_pdf_via_browser(html: str) -> bytes:
    browser_path = _pick_browser()
    tmp_root = BASE_DIR / ".tmp" / "pdf-export"
    tmp_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="memo-", dir=str(tmp_root)) as tmp_dir:
        tmp_path = Path(tmp_dir)
        profile_dir = tmp_path / "browser-profile"
        html_path = tmp_path / "memo-export.html"
        pdf_path = tmp_path / "memo-export.pdf"
        profile_dir.mkdir(parents=True, exist_ok=True)
        html_path.write_text(html, encoding="utf-8")

        command = _chrome_pdf_command(browser_path, profile_dir, html_path, pdf_path)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=RENDER_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PDFRenderError("Browser PDF export timed out.") from exc

        if completed.returncode != 0:
            stderr_tail = (completed.stderr or completed.stdout or "").strip()[-1200:]
            raise PDFRenderError(f"Browser PDF export failed with exit code {completed.returncode}: {stderr_tail}")
        if not pdf_path.exists():
            raise PDFRenderError("Browser PDF export did not produce an output file.")

        pdf_bytes = pdf_path.read_bytes()
        if not pdf_bytes.startswith(b"%PDF"):
            raise PDFRenderError("Generated file is not a valid PDF stream.")
        return pdf_bytes


@lru_cache(maxsize=1)
def _memo_pdf_font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont, TTFError

    for filename in ("msyh.ttc", "simsun.ttc", "simhei.ttf"):
        font_path = Path(r"C:\Windows\Fonts") / filename
        if font_path.exists():
            try:
                pdfmetrics.registerFont(TTFont("LegalMemoCJK", str(font_path)))
                return "LegalMemoCJK"
            except TTFError:
                continue
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light"


def _render_analysis_pdf(html: str) -> bytes:
    from bs4 import BeautifulSoup
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate

    article = BeautifulSoup(html, "html.parser").select_one(".memo-sheet")
    if article is None:
        raise PDFRenderError("The analysis memo has no exportable content.")
    font_name = _memo_pdf_font()
    body = ParagraphStyle(
        "MemoBody", fontName=font_name, fontSize=10, leading=17,
        wordWrap="CJK", spaceAfter=7,
    )
    heading = ParagraphStyle(
        "MemoHeading", parent=body, fontSize=13, leading=20,
        spaceBefore=12, keepWithNext=True,
    )
    title = ParagraphStyle("MemoTitle", parent=heading, fontSize=20, leading=28)
    styles = {"h1": title, "h2": heading, "h3": heading}
    paragraphs = []
    for element in article.select("h1, h2, h3, p, li"):
        content = element.get_text(" ", strip=True)
        if content:
            paragraphs.append(Paragraph(escape(content), styles.get(element.name, body)))
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=16*mm, rightMargin=16*mm,
        topMargin=14*mm, bottomMargin=14*mm, title="Legal analysis memo",
    )
    document.build(paragraphs)
    return buffer.getvalue()


def render_legal_memo_pdf(payload: dict) -> bytes:
    if not (payload.get("prediction_result") or payload.get("memo_analysis")):
        raise PDFRenderError("An analysis or prediction is required for PDF export.")

    html = _render_export_html(payload)
    if payload.get("prediction_result"):
        return _render_pdf_via_browser(html)
    return _render_analysis_pdf(html)
