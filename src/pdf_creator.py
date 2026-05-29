"""コメントページPDF生成モジュール。reportlabで日本語対応のコメントページを作成する。"""

from __future__ import annotations

import logging
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from src.config import FONT_REGULAR, FONT_BOLD, JISSEN_KUN_IMAGE
from src.utils import ensure_fonts

logger = logging.getLogger("jissen_comment")

# ── レイアウト定数 ──
PAGE_WIDTH, PAGE_HEIGHT = A4  # 210mm x 297mm
MARGIN = 20 * mm
BOX_MARGIN = 25 * mm
BORDER_COLOR = HexColor("#CCCCCC")
TITLE_FONT_SIZE = 18
NAME_FONT_SIZE = 13
BODY_FONT_SIZE = 11
LINE_HEIGHT = BODY_FONT_SIZE * 1.8

_fonts_registered = False


def _register_fonts() -> None:
    """NotoSansJPフォントを登録する（初回のみ）。"""
    global _fonts_registered
    if _fonts_registered:
        return

    ensure_fonts()

    pdfmetrics.registerFont(TTFont("NotoSansJP", str(FONT_REGULAR)))
    pdfmetrics.registerFont(TTFont("NotoSansJP-Bold", str(FONT_BOLD)))
    _fonts_registered = True


def _wrap_text(text: str, font_name: str, font_size: float, max_width: float) -> list[str]:
    """テキストを指定幅で折り返す。日本語文字幅を考慮。"""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue

        current_line = ""
        for char in paragraph:
            test_line = current_line + char
            width = pdfmetrics.stringWidth(test_line, font_name, font_size)
            if width > max_width:
                lines.append(current_line)
                current_line = char
            else:
                current_line = test_line
        if current_line:
            lines.append(current_line)

    return lines


def create_comment_page(
    comment: str,
    clinic_name: str,
    person_name: str,
    output_path: str | Path,
) -> Path:
    """コメントページPDFを1ページ生成する。

    Args:
        comment: コメント本文（200〜350文字）
        clinic_name: 医院名
        person_name: 氏名
        output_path: 出力先PDFパス

    Returns:
        生成されたPDFのパス
    """
    _register_fonts()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(output_path), pagesize=A4)

    # ── 外枠（グレー罫線） ──
    box_x = BOX_MARGIN
    box_y = BOX_MARGIN
    box_w = PAGE_WIDTH - 2 * BOX_MARGIN
    box_h = PAGE_HEIGHT - 2 * BOX_MARGIN

    c.setStrokeColor(BORDER_COLOR)
    c.setLineWidth(1)
    c.rect(box_x, box_y, box_w, box_h)

    # ── コンテンツエリア ──
    content_x = box_x + 15 * mm
    content_w = box_w - 30 * mm
    y = box_y + box_h - 20 * mm  # 枠上端からの開始位置

    # ── タイトル（中央揃え・太字） ──
    c.setFont("NotoSansJP-Bold", TITLE_FONT_SIZE)
    title = "じっせん君からのコメント"
    title_width = pdfmetrics.stringWidth(title, "NotoSansJP-Bold", TITLE_FONT_SIZE)
    c.drawString(PAGE_WIDTH / 2 - title_width / 2, y, title)
    y -= 10 * mm

    # ── 区切り線 ──
    line_x_start = content_x
    line_x_end = content_x + content_w
    c.setStrokeColor(BORDER_COLOR)
    c.setLineWidth(0.5)
    c.line(line_x_start, y, line_x_end, y)
    y -= 12 * mm

    # コメント本文へ。受領者の識別情報（医院名・氏名）はコメントページ上に
    # 描画しない（個人情報をコメント上に残さない方針）。clinic_name /
    # person_name は出力ファイル名・Drive フォルダ階層・出力一覧シートでのみ使う。

    # ── コメント本文（左揃え・折り返し） ──
    c.setFont("NotoSansJP", BODY_FONT_SIZE)
    c.setFillColor(HexColor("#333333"))

    wrapped_lines = _wrap_text(comment, "NotoSansJP", BODY_FONT_SIZE, content_w)

    for line in wrapped_lines:
        if y < box_y + 40 * mm:  # じっせん君画像分の余白を確保
            break
        c.drawString(content_x, y, line)
        y -= LINE_HEIGHT

    # ── じっせん君画像（右下） ──
    if JISSEN_KUN_IMAGE.exists():
        img_width = 35 * mm
        img_height = 35 * mm
        img_x = box_x + box_w - 15 * mm - img_width
        img_y = box_y + 10 * mm
        try:
            c.drawImage(
                str(JISSEN_KUN_IMAGE),
                img_x, img_y,
                width=img_width, height=img_height,
                preserveAspectRatio=True,
                mask="auto",
            )
        except Exception as e:
            logger.warning(f"じっせん君画像の描画に失敗: {e}")
    else:
        logger.warning(f"じっせん君画像が見つかりません: {JISSEN_KUN_IMAGE}")

    c.save()
    logger.info(f"コメントページPDF作成: {output_path}")
    return output_path
