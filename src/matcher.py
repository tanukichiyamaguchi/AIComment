"""PDFテキストとスプレッドシートのマッチングモジュール。"""

from __future__ import annotations

import logging
import re
import unicodedata

from src.sheets_client import ClinicRecord

logger = logging.getLogger("jissen_comment")


def _normalize(text: str) -> str:
    """テキストを正規化する（全角→半角、スペース除去など）。"""
    # Unicode正規化（NFKC: 全角英数→半角、半角カナ→全角カナ）
    text = unicodedata.normalize("NFKC", text)
    # スペース系を統一
    text = re.sub(r"\s+", "", text)
    # 小文字化
    text = text.lower()
    return text


def extract_clinic_info(pdf_text: str) -> dict[str, str | None]:
    """PDFテキストから医院名・氏名を正規表現で抽出する。

    Args:
        pdf_text: PDFから抽出したテキスト

    Returns:
        {"clinic_name": str | None, "person_name": str | None}
    """
    result: dict[str, str | None] = {"clinic_name": None, "person_name": None}

    # 医院名の抽出パターン
    clinic_patterns = [
        r"医院名[:\s：\s]*(.+?)[\s\n]",
        r"(?:歯科)?医院[:\s：\s]*(.+?)[\s\n]",
        r"(.+?歯科(?:医院|クリニック))",
        r"(.+?デンタル(?:クリニック|オフィス))",
    ]

    for pattern in clinic_patterns:
        match = re.search(pattern, pdf_text)
        if match:
            result["clinic_name"] = match.group(1).strip()
            break

    # 氏名の抽出パターン
    name_patterns = [
        r"(?:氏名|名前|お名前|記入者)[:\s：\s]*(.+?)[\s\n]",
        r"(?:院長|代表)[:\s：\s]*(.+?)[\s\n]",
    ]

    for pattern in name_patterns:
        match = re.search(pattern, pdf_text)
        if match:
            result["person_name"] = match.group(1).strip()
            break

    logger.info(
        f"PDF情報抽出: 医院名={result['clinic_name']}, "
        f"氏名={result['person_name']}"
    )
    return result


def match_record(
    pdf_text: str,
    records: list[ClinicRecord],
    pdf_filename: str = "",
) -> ClinicRecord | None:
    """PDFテキストまたはファイル名からスプレッドシートのレコードとマッチングする。

    マッチング優先順位:
    1. 医院名の完全一致（正規化後）
    2. 医院名の部分一致
    3. 氏名の完全一致（正規化後）
    4. 氏名の部分一致
    5. ファイル名に医院名または氏名が含まれる

    Args:
        pdf_text: PDFから抽出したテキスト
        records: スプレッドシートのレコードリスト
        pdf_filename: PDFファイル名（追加マッチング用）

    Returns:
        マッチしたClinicRecord、またはNone
    """
    if not records:
        return None

    pdf_info = extract_clinic_info(pdf_text)
    pdf_clinic = pdf_info.get("clinic_name") or ""
    pdf_person = pdf_info.get("person_name") or ""

    normalized_pdf_text = _normalize(pdf_text)
    normalized_filename = _normalize(pdf_filename)

    # スコアリング
    best_match: ClinicRecord | None = None
    best_score = 0

    for record in records:
        score = 0
        norm_clinic = _normalize(record.clinic_name)
        norm_person = _normalize(record.person_name)

        # 医院名マッチング
        if pdf_clinic:
            norm_pdf_clinic = _normalize(pdf_clinic)
            if norm_pdf_clinic == norm_clinic:
                score += 100  # 完全一致
            elif norm_clinic in norm_pdf_clinic or norm_pdf_clinic in norm_clinic:
                score += 80  # 部分一致

        # テキスト全体での医院名検索
        if norm_clinic in normalized_pdf_text:
            score += 60

        # 氏名マッチング
        if pdf_person:
            norm_pdf_person = _normalize(pdf_person)
            if norm_pdf_person == norm_person:
                score += 50
            elif norm_person in norm_pdf_person or norm_pdf_person in norm_person:
                score += 30

        # テキスト全体での氏名検索
        if norm_person in normalized_pdf_text:
            score += 20

        # ファイル名でのマッチング
        if normalized_filename:
            if norm_clinic in normalized_filename:
                score += 40
            if norm_person in normalized_filename:
                score += 30

        if score > best_score:
            best_score = score
            best_match = record

    if best_match and best_score >= 20:
        logger.info(
            f"マッチング成功: {best_match.clinic_name} {best_match.person_name} "
            f"(スコア: {best_score})"
        )
        return best_match

    logger.warning(
        f"マッチング失敗: PDF医院名={pdf_clinic}, PDF氏名={pdf_person}, "
        f"ファイル名={pdf_filename}"
    )
    return None
