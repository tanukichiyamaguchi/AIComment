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

# じっせん君画像（右下配置）のジオメトリ。本文の描画下限はこの画像帯から導出
# する（定数を重複させると、画像サイズ変更時に本文と画像が重なる不変式が
# 黙って壊れるため）。
_IMG_SIZE = 35 * mm
_IMG_BOTTOM_OFFSET = 10 * mm  # 枠下端から画像下端までの余白
_BODY_BOTTOM_CLEARANCE = 5 * mm  # 本文最下行と画像上端の間に確保する余白

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


# 行頭禁則文字（行頭に来てはいけない文字）。これらの文字が次の行の先頭に
# 来そうになったとき、1 文字ぶら下げて前の行末尾に残す（はみ出しを許容する
# 簡易禁則）。コメント末尾の「。」や読点「、」が単独で行頭に来る不自然さ
# （日本語組版の基本ルール違反）を防ぐ。
_GYOTOU_KINSOKU: frozenset[str] = frozenset(
    "、。，．・：；！？）］｝」』〕〉》】〙〗"
    ",.!?)]}"
)


def _wrap_text(text: str, font_name: str, font_size: float, max_width: float) -> list[str]:
    """テキストを指定幅で折り返す。日本語文字幅 + 行頭禁則を考慮。

    ``text`` に含まれる ``\\n`` は段落区切りとして扱い、段落ごとに独立して
    折り返す（Claude が文脈の切れ目で挿入した改行をそのまま尊重する）。
    各段落内では、行頭禁則文字（「、」「。」「！」「？」「)」など）が次行の
    先頭に来そうな場合、その 1 文字を前行末尾にぶら下げて簡易禁則を実現する。
    """
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
                # 行頭禁則: 改行直後の先頭になる ``char`` が禁則文字なら、
                # 前行末尾にぶら下げて改行する（はみ出しを許容）。前行が
                # 空（=1文字目から幅超過の異常系）の場合はぶら下げない。
                if char in _GYOTOU_KINSOKU and current_line:
                    lines.append(current_line + char)
                    current_line = ""
                else:
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

    # 本文の描画下限。じっせん君画像は箱下端 +_IMG_BOTTOM_OFFSET から
    # _IMG_SIZE の高さを占有するため、その上端 + クリアランスより下には
    # 本文を描かない（長文時に最下行が画像と重なる問題の回避）。
    # 旧実装の固定値 box_y + 40mm は画像上端（box_y + 45mm）より低く、
    # 満幅の最下行が画像に食い込み得た。
    body_min_y = box_y + _IMG_BOTTOM_OFFSET + _IMG_SIZE + _BODY_BOTTOM_CLEARANCE

    drawn_lines = 0
    for line in wrapped_lines:
        if y < body_min_y:
            break
        c.drawString(content_x, y, line)
        y -= LINE_HEIGHT
        drawn_lines += 1

    # 描画領域に収まらず切り捨てた行があれば loud に警告する（P-001: 無言の
    # 欠落は事故）。通常のコメント（200〜350 文字 + 段落改行）は収まる設計
    # だが、想定超の長文・多段落で本文が黙って欠けたまま出荷されるのを防ぐ。
    if drawn_lines < len(wrapped_lines):
        logger.warning(
            f"コメントが描画領域に収まらず {len(wrapped_lines) - drawn_lines} 行"
            f"切り捨てました（全 {len(wrapped_lines)} 行, {len(comment)} 文字）。"
            f"PDF 上でコメント末尾が欠けています: {output_path.name}"
        )

    # ── じっせん君画像（右下） ──
    if JISSEN_KUN_IMAGE.exists():
        img_width = _IMG_SIZE
        img_height = _IMG_SIZE
        img_x = box_x + box_w - 15 * mm - img_width
        img_y = box_y + _IMG_BOTTOM_OFFSET
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
