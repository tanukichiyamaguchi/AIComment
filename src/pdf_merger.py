"""PDF結合モジュール。元PDFの末尾にコメントページを1ページ追加する。"""

from __future__ import annotations

import io
import logging
from pathlib import Path

from pypdf import PdfReader, PdfWriter

logger = logging.getLogger("jissen_comment")


def merge_pdfs(
    original_pdf_data: bytes,
    comment_page_path: str | Path,
    output_path: str | Path,
) -> Path:
    """元PDFの末尾にコメントページPDFを1ページ追加する。

    元PDFのページは一切変更しない。

    Args:
        original_pdf_data: 元PDFのバイナリデータ
        comment_page_path: コメントページPDFのパス
        output_path: 結合後PDFの出力先パス

    Returns:
        結合後PDFのパス
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    writer = PdfWriter()

    # 元PDFの全ページを追加
    original_reader = PdfReader(io.BytesIO(original_pdf_data))
    original_pages = len(original_reader.pages)
    for page in original_reader.pages:
        writer.add_page(page)

    # コメントページを追加
    comment_reader = PdfReader(str(comment_page_path))
    writer.add_page(comment_reader.pages[0])

    # 書き出し
    with open(output_path, "wb") as f:
        writer.write(f)

    total_pages = original_pages + 1
    logger.info(
        f"PDF結合完了: {original_pages}ページ + 1ページ = {total_pages}ページ → {output_path}"
    )
    return output_path


# OS ファイル名上限。ext4 / NTFS / FAT は 255 バイト、APFS は 255 UTF-16
# コードユニット。最も厳しい ext4 の 255 バイトに揃える。
# lessons.md P-011 参照。
_FILENAME_SEPARATOR = "＿"
_FILENAME_EXTENSION = ".pdf"
_FILENAME_MAX_BYTES = 255


def make_output_filename(clinic_name: str, person_name: str, sample_title: str) -> str:
    """出力PDFのファイル名を生成する。

    形式: ``<医院名>＿<個人名>＿<実践事例タイトル>.pdf``
    （区切りは全角アンダースコア ``＿``）

    OS 制限の 255 バイトを超えないよう、UTF-8 バイト長ベースで各セクションを
    均等に切り詰める。短い名前はそのまま、長い名前だけが削られる。

    また、``sample_title`` に既に ``.pdf``（大小区別なし）が含まれる場合は
    取り除いてから合成し、``.pdf.pdf`` のような二重拡張子を防ぐ
    （lessons.md P-012 参照）。
    """
    safe_clinic = _sanitize_filename(clinic_name)
    safe_person = _sanitize_filename(person_name)
    safe_title = _sanitize_filename(sample_title)

    # 拡張子は make_output_filename が単一の付与責務を持つ。入力に含まれて
    # いれば一度だけ取り除いて、最後に ``.pdf`` を 1 回だけ付与する。
    if safe_title.lower().endswith(_FILENAME_EXTENSION):
        safe_title = safe_title[: -len(_FILENAME_EXTENSION)] or "unknown"

    # 区切り文字 x 2 と拡張子の固定オーバーヘッドを差し引いた残りを
    # 3 セクションに割り当てる。
    overhead = (
        len(_FILENAME_SEPARATOR.encode("utf-8")) * 2
        + len(_FILENAME_EXTENSION.encode("utf-8"))
    )
    budget = _FILENAME_MAX_BYTES - overhead
    per_section = budget // 3
    safe_clinic = _truncate_to_bytes(safe_clinic, per_section)
    safe_person = _truncate_to_bytes(safe_person, per_section)
    # 残バイトを title に回す（clinic / person が予算未満なら title に余裕）。
    title_budget = budget - len(safe_clinic.encode("utf-8")) - len(safe_person.encode("utf-8"))
    safe_title = _truncate_to_bytes(safe_title, title_budget)

    filename = f"{safe_clinic}{_FILENAME_SEPARATOR}{safe_person}{_FILENAME_SEPARATOR}{safe_title}{_FILENAME_EXTENSION}"
    # 安全側の不変条件: OS 上限を超えないこと。
    assert len(filename.encode("utf-8")) <= _FILENAME_MAX_BYTES, (
        f"内部不変条件違反: ファイル名が {len(filename.encode('utf-8'))} バイト"
    )
    return filename


def _truncate_to_bytes(s: str, max_bytes: int) -> str:
    """UTF-8 バイト長で ``max_bytes`` 以内に切り詰める（文字境界を守る）。

    ``encode → 末尾切り → decode`` を試行し、不完全な UTF-8 シーケンスが
    残った場合は 1 バイトずつ削って境界を見つける。空入力や予算 0 以下の
    場合は ``"unknown"`` の先頭 ``max_bytes`` バイトで代替する（
    sanitize 側の fallback と整合）。
    """
    if max_bytes <= 0:
        # 物理的に格納不可能。""よりは "?" 等の 1 文字を許容する仕様もあるが、
        # ここでは予算超過自体が make_output_filename 側のロジックエラーなので
        # 空文字を返して呼び出し側の assertion を意図的に発火させる。
        return ""
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    truncated = encoded[:max_bytes]
    while truncated:
        try:
            return truncated.decode("utf-8")
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    return ""


def _sanitize_filename(name: str) -> str:
    """ファイル名に使用できない文字を除去し、パストラバーサルを防止する。"""
    import re
    # Remove path separators and null bytes
    name = name.replace("/", "").replace("\\", "").replace("\x00", "")
    # Remove . and .. components
    name = name.strip(".")
    # Remove other OS-unsafe characters
    name = re.sub(r'[<>:"|?*]', "", name)
    return name or "unknown"
