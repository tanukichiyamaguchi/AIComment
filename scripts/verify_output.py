"""出力PDFサンプル検証スクリプト。コメントページ無しPDFが顧客に届くのを防ぐ。

エージェント `output-verifier` から呼び出される。

検査項目:
  1. PDFが例外なく開ける
  2. ページ数 = 元PDFページ数 + 1
  3. 最終ページに50字以上の日本語が存在する（フォント失敗検出）
  4. 最終ページのテキストがコメント本文と70%以上一致する
  5. ファイルサイズが 100KB ≤ size ≤ 25MB

使い方:
  python -m scripts.verify_output --sample-rate 0.05
  python -m scripts.verify_output --output-dir logs/outputs --results logs/batch_results.json
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import random
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import pdfplumber
from pypdf import PdfReader

from src.config import LOGS_DIR

logger = logging.getLogger("jissen_comment.verify")

MIN_SAMPLE = 30
MIN_FILE_BYTES = 100 * 1024  # 100KB
MAX_FILE_BYTES = 25 * 1024 * 1024  # 25MB Gmail limit
MIN_LAST_PAGE_CHARS = 50
TEXT_OVERLAP_THRESHOLD = 0.70
DEFAULT_FAIL_RATE_BLOCK = 0.01  # 1%

JAPANESE_RE = re.compile(r"[぀-ゟ゠-ヿ一-鿿]")


@dataclass
class VerifyEntry:
    path: str
    passed: bool
    failures: list[str]
    page_count: int
    last_page_jp_chars: int
    overlap_ratio: float
    size_bytes: int


def _tokenize(text: str) -> set[str]:
    """日本語文字を1文字単位でトークン化（n-gram的fuzzyマッチ用）。"""
    return set(JAPANESE_RE.findall(text))


def _overlap_ratio(expected: str, actual: str) -> float:
    """expected の文字集合が actual に含まれる割合。"""
    e = _tokenize(expected)
    if not e:
        return 1.0  # nothing to compare
    a = _tokenize(actual)
    return len(e & a) / len(e)


def verify_one(
    pdf_path: Path,
    expected_comment: str | None,
    expected_original_pages: int | None,
) -> VerifyEntry:
    """1件のPDFを検証する。"""
    failures: list[str] = []
    size = pdf_path.stat().st_size if pdf_path.exists() else 0

    if not pdf_path.exists():
        return VerifyEntry(
            path=str(pdf_path), passed=False, failures=["file_missing"],
            page_count=0, last_page_jp_chars=0, overlap_ratio=0.0, size_bytes=0,
        )

    if size < MIN_FILE_BYTES:
        failures.append(f"undersized:{size}")
    if size > MAX_FILE_BYTES:
        failures.append(f"oversized:{size}")

    page_count = 0
    last_page_text = ""
    try:
        reader = PdfReader(str(pdf_path))
        page_count = len(reader.pages)
    except Exception as e:
        failures.append(f"unopenable:{type(e).__name__}")
        return VerifyEntry(
            path=str(pdf_path), passed=False, failures=failures,
            page_count=0, last_page_jp_chars=0, overlap_ratio=0.0, size_bytes=size,
        )

    if expected_original_pages is not None:
        expected_total = expected_original_pages + 1
        if page_count != expected_total:
            failures.append(f"page_count:{page_count}!={expected_total}")

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            if pdf.pages:
                last_page_text = pdf.pages[-1].extract_text() or ""
    except Exception as e:
        failures.append(f"last_page_unreadable:{type(e).__name__}")

    jp_chars = len(JAPANESE_RE.findall(last_page_text))
    if jp_chars < MIN_LAST_PAGE_CHARS:
        failures.append(f"insufficient_jp_chars:{jp_chars}")

    overlap = 0.0
    if expected_comment:
        overlap = _overlap_ratio(expected_comment, last_page_text)
        if overlap < TEXT_OVERLAP_THRESHOLD:
            failures.append(f"comment_overlap:{overlap:.2f}")

    return VerifyEntry(
        path=str(pdf_path), passed=not failures, failures=failures,
        page_count=page_count, last_page_jp_chars=jp_chars,
        overlap_ratio=overlap, size_bytes=size,
    )


def select_sample(paths: list[Path], sample_rate: float, seed: int = 0) -> list[Path]:
    """確定的サンプリング（最低 MIN_SAMPLE、上限は全件）。"""
    n = max(MIN_SAMPLE, int(len(paths) * sample_rate))
    n = min(n, len(paths))
    rng = random.Random(seed)
    return rng.sample(paths, n) if paths else []


def verify_batch(
    output_dir: Path,
    results_json: Path | None,
    prep_json: Path | None,
    sample_rate: float,
    seed: int = 0,
) -> tuple[list[VerifyEntry], dict]:
    """バッチ全体のサンプル検証を行う。"""
    pdf_paths = sorted(output_dir.glob("*.pdf"))
    if not pdf_paths:
        return [], {"sampled": 0, "passed": 0, "failed": 0, "fail_rate": 0.0}

    # Build expected comment / original page count lookup (best-effort)
    comments: dict[str, str] = {}
    original_pages: dict[str, int] = {}
    if results_json and results_json.exists():
        results = json.loads(results_json.read_text())
        # results may be {custom_id: comment} or {custom_id: {comment, ...}}
        for k, v in results.items():
            comments[k] = v if isinstance(v, str) else v.get("comment", "")
    if prep_json and prep_json.exists():
        prep = json.loads(prep_json.read_text())
        for item in prep:
            if "original_pages" in item and "custom_id" in item:
                original_pages[item["custom_id"]] = item["original_pages"]

    sample = select_sample(pdf_paths, sample_rate, seed)
    entries: list[VerifyEntry] = []
    for p in sample:
        # naive lookup: filename contains person_name; try matching by prefix
        cid = next((k for k in comments if k in p.stem), None)
        entry = verify_one(
            p,
            expected_comment=comments.get(cid) if cid else None,
            expected_original_pages=original_pages.get(cid) if cid else None,
        )
        entries.append(entry)

    passed = sum(1 for e in entries if e.passed)
    failed = len(entries) - passed
    fail_rate = failed / len(entries) if entries else 0.0
    summary = {
        "sampled": len(entries),
        "total": len(pdf_paths),
        "passed": passed,
        "failed": failed,
        "fail_rate": fail_rate,
    }
    return entries, summary


def write_report(entries: list[VerifyEntry], summary: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "summary": summary,
        "entries": [asdict(e) for e in entries],
    }, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Sample-verify merged PDFs")
    parser.add_argument("--output-dir", type=Path, default=LOGS_DIR / "outputs")
    parser.add_argument("--results", type=Path, default=LOGS_DIR / "batch_results.json")
    parser.add_argument("--prep", type=Path, default=LOGS_DIR / "batch_prep.json")
    parser.add_argument("--sample-rate", type=float, default=0.05)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--report", type=Path, default=LOGS_DIR / "verify_report.json")
    parser.add_argument("--block-threshold", type=float, default=DEFAULT_FAIL_RATE_BLOCK)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    entries, summary = verify_batch(
        output_dir=args.output_dir,
        results_json=args.results,
        prep_json=args.prep,
        sample_rate=args.sample_rate,
        seed=args.seed,
    )
    write_report(entries, summary, args.report)

    decision = "APPROVE_SEND" if summary["fail_rate"] <= args.block_threshold else "BLOCK_SEND"

    print("OUTPUT VERIFICATION")
    print(f"  Sampled:       {summary['sampled']} / {summary.get('total', 0)}")
    print(f"  Passed:        {summary['passed']}")
    print(f"  Failed:        {summary['failed']}")
    print(f"  Failure rate:  {summary['fail_rate']:.2%}")
    print(f"  Decision:      {decision}")
    print(f"  Threshold:     {args.block_threshold:.0%}")
    print(f"  Report:        {args.report}")

    return 0 if decision == "APPROVE_SEND" else 4


if __name__ == "__main__":
    raise SystemExit(main())
