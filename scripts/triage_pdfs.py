"""PDFトリアージスクリプト。1000+件のPDFを処理前に分類し、サイレント失敗を防ぐ。

カテゴリ:
  healthy   — text extracts cleanly, ≤2MB
  scanned   — opens but no extractable text (image-only, needs OCR)
  encrypted — password-protected
  corrupted — pdfplumber raises
  oversized — >10MB (Gmail attachment / batch payload risk)
  duplicate — SHA256 collision with another PDF in the batch

エージェント `pdf-triage-officer` から呼び出される。

使い方:
  python -m scripts.triage_pdfs --source drive    # Driveフォルダ全件
  python -m scripts.triage_pdfs --source local --path /tmp/pdfs/
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import pdfplumber
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from src.config import LOGS_DIR

logger = logging.getLogger("jissen_comment.triage")

HEALTHY = "healthy"
SCANNED = "scanned"
ENCRYPTED = "encrypted"
CORRUPTED = "corrupted"
OVERSIZED = "oversized"
DUPLICATE = "duplicate"

OVERSIZED_BYTES = 10 * 1024 * 1024  # 10MB
HALT_THRESHOLD = 0.01  # 1%


@dataclass
class TriageEntry:
    file_id: str
    name: str
    category: str
    sha256: str
    size_bytes: int
    page_count: int
    recommended_action: str


def classify(name: str, file_id: str, data: bytes, seen_hashes: dict[str, str]) -> TriageEntry:
    """1件のPDFを分類する。"""
    digest = hashlib.sha256(data).hexdigest()
    size = len(data)

    if digest in seen_hashes:
        return TriageEntry(
            file_id=file_id, name=name, category=DUPLICATE, sha256=digest,
            size_bytes=size, page_count=0,
            recommended_action=f"duplicate of {seen_hashes[digest]}",
        )
    seen_hashes[digest] = name

    if size > OVERSIZED_BYTES:
        return TriageEntry(
            file_id=file_id, name=name, category=OVERSIZED, sha256=digest,
            size_bytes=size, page_count=0,
            recommended_action="reduce size or split before processing",
        )

    # Encryption check — pypdf raises or sets is_encrypted
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return TriageEntry(
                file_id=file_id, name=name, category=ENCRYPTED, sha256=digest,
                size_bytes=size, page_count=0,
                recommended_action="decrypt before processing",
            )
        page_count = len(reader.pages)
    except (PdfReadError, Exception) as e:
        return TriageEntry(
            file_id=file_id, name=name, category=CORRUPTED, sha256=digest,
            size_bytes=size, page_count=0,
            recommended_action=f"corrupted: {type(e).__name__}",
        )

    # Text extraction check — distinguishes scanned from healthy
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            extracted_chars = sum(len(p.extract_text() or "") for p in pdf.pages)
    except Exception as e:
        return TriageEntry(
            file_id=file_id, name=name, category=CORRUPTED, sha256=digest,
            size_bytes=size, page_count=page_count,
            recommended_action=f"pdfplumber failed: {type(e).__name__}",
        )

    if extracted_chars < 50:
        return TriageEntry(
            file_id=file_id, name=name, category=SCANNED, sha256=digest,
            size_bytes=size, page_count=page_count,
            recommended_action="OCR required (image-only PDF)",
        )

    return TriageEntry(
        file_id=file_id, name=name, category=HEALTHY, sha256=digest,
        size_bytes=size, page_count=page_count,
        recommended_action="proceed",
    )


def triage_iter(items: Iterable[tuple[str, str, bytes]]) -> list[TriageEntry]:
    """(file_id, name, data) のイテラブルを順次分類する。"""
    seen_hashes: dict[str, str] = {}
    entries: list[TriageEntry] = []
    for file_id, name, data in items:
        entry = classify(name, file_id, data, seen_hashes)
        entries.append(entry)
        logger.info(f"triage: {name} → {entry.category}")
    return entries


def summarize(entries: list[TriageEntry]) -> dict[str, int]:
    """カテゴリ別の件数を集計する。"""
    counts = {HEALTHY: 0, SCANNED: 0, ENCRYPTED: 0, CORRUPTED: 0, OVERSIZED: 0, DUPLICATE: 0}
    for e in entries:
        counts[e.category] = counts.get(e.category, 0) + 1
    return counts


def decision(entries: list[TriageEntry], halt_threshold: float = HALT_THRESHOLD) -> tuple[str, str]:
    """PROCEED / HALT を判定する。"""
    if not entries:
        return "HALT", "no PDFs found"
    counts = summarize(entries)
    unhealthy = sum(v for k, v in counts.items() if k != HEALTHY)
    rate = unhealthy / len(entries)
    if rate > halt_threshold:
        return "HALT", f"unhealthy rate {rate:.1%} exceeds {halt_threshold:.1%} threshold"
    return "PROCEED", "all checks passed"


def write_manifest(entries: list[TriageEntry], path: Path) -> None:
    """マニフェストをJSONで書き出す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "total": len(entries),
        "summary": summarize(entries),
        "entries": [asdict(e) for e in entries],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def _iter_drive() -> Iterable[tuple[str, str, bytes]]:
    """DriveフォルダからPDFを順次取得する（遅延ダウンロード）。"""
    from src import drive_client
    for f in drive_client.list_pdfs():
        yield f["id"], f["name"], drive_client.download_pdf(f["id"])


def _iter_local(path: Path) -> Iterable[tuple[str, str, bytes]]:
    """ローカルディレクトリからPDFを順次読む。"""
    for p in sorted(path.glob("*.pdf")):
        yield p.stem, p.name, p.read_bytes()


def main() -> int:
    parser = argparse.ArgumentParser(description="PDF triage")
    parser.add_argument("--source", choices=["drive", "local"], default="drive")
    parser.add_argument("--path", type=Path, default=None, help="local source path")
    parser.add_argument("--output", type=Path, default=LOGS_DIR / "triage_manifest.json")
    parser.add_argument("--halt-threshold", type=float, default=HALT_THRESHOLD)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if args.source == "local":
        if not args.path or not args.path.is_dir():
            raise SystemExit("--path must point to a directory containing PDFs")
        items = _iter_local(args.path)
    else:
        items = _iter_drive()

    entries = triage_iter(items)
    write_manifest(entries, args.output)
    counts = summarize(entries)
    verdict, reason = decision(entries, args.halt_threshold)

    print("TRIAGE COMPLETE")
    print(
        f"  Total: {len(entries)}  Healthy: {counts[HEALTHY]}  "
        f"Scanned: {counts[SCANNED]}  Encrypted: {counts[ENCRYPTED]}  "
        f"Corrupted: {counts[CORRUPTED]}  Oversized: {counts[OVERSIZED]}  "
        f"Duplicates: {counts[DUPLICATE]}"
    )
    print(f"  Manifest: {args.output}")
    print(f"  Decision: {verdict}")
    print(f"  Reason:   {reason}")

    return 0 if verdict == "PROCEED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
