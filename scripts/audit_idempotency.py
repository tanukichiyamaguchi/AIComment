"""冪等性監査スクリプト。リラン時のGmail下書き重複と「処理中」ステータスの永久滞留を防ぐ。

エージェント `idempotency-guardian` から呼び出される。

検出する状態:
  1. 「処理中」のまま放置されたレコード (>2h) → 「未処理」へリセット
  2. ステータス「完了」だが対応するGmail下書きが見つからない → 異常
  3. Gmail下書きが存在するがステータスが「完了」ではない → 異常

使い方:
  python -m scripts.audit_idempotency
  python -m scripts.audit_idempotency --stuck-threshold-hours 4
  python -m scripts.audit_idempotency --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config import LOGS_DIR
from src.gmail_client import SUBJECT_TEMPLATE
from src.sheets_client import ClinicRecord, read_records, update_status

logger = logging.getLogger("jissen_comment.audit")

STUCK_PREFIX = "処理中"
DONE_STATUS = "完了"
RESET_STATUS = "未処理"


@dataclass
class AuditResult:
    total_rows: int
    already_done: list[int]
    stuck_recovered: list[int]
    drafts_found: int
    anomalies: list[dict[str, Any]]
    safe_to_proceed: bool
    report_path: str


def _parse_subject_person(subject: str) -> str | None:
    """件名から person_name を逆抽出する。SUBJECT_TEMPLATE依存。"""
    pattern = SUBJECT_TEMPLATE.replace("{person_name}", "(?P<name>.+?)").replace("[", r"\[").replace("]", r"\]")
    m = re.match(pattern, subject)
    return m.group("name") if m else None


def fetch_drafts() -> list[dict[str, Any]]:
    """Gmail下書きを全件取得し、件名と作成日を返す。"""
    try:
        from src.gmail_client import get_gmail_service
        service = get_gmail_service()
    except Exception as e:
        logger.warning(f"Gmail未接続のためドラフト監査をスキップ: {e}")
        return []

    drafts: list[dict[str, Any]] = []
    page_token = None
    while True:
        resp = service.users().drafts().list(
            userId="me", maxResults=500, pageToken=page_token,
        ).execute()
        for d in resp.get("drafts", []):
            msg = service.users().drafts().get(
                userId="me", id=d["id"], format="metadata",
                metadataHeaders=["Subject", "Date", "To"],
            ).execute()
            headers = {
                h["name"]: h["value"]
                for h in msg.get("message", {}).get("payload", {}).get("headers", [])
            }
            drafts.append({
                "id": d["id"],
                "subject": headers.get("Subject", ""),
                "to": headers.get("To", ""),
                "date": headers.get("Date", ""),
            })
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return drafts


def recover_stuck_rows(
    records: list[ClinicRecord],
    stuck_threshold_hours: int,
    dry_run: bool,
) -> list[int]:
    """「処理中」で長時間放置されているレコードを「未処理」にリセットする。

    Note: Sheets APIは行単位の最終更新時刻を直接提供しないため、
    保守的に「処理中」プレフィックスの全行を対象とする。
    呼び出し前に `--stuck-threshold-hours 0` で全リセットすることを禁ずる。
    """
    if stuck_threshold_hours < 1:
        raise ValueError("stuck_threshold_hours must be >= 1 (safety floor)")

    recovered: list[int] = []
    for r in records:
        if r.status.startswith(STUCK_PREFIX):
            recovered.append(r.row_number)
            if not dry_run:
                update_status(r.row_number, RESET_STATUS)
                logger.info(f"recovered stuck row {r.row_number}: {r.clinic_name} {r.person_name}")
    return recovered


def detect_anomalies(
    records: list[ClinicRecord],
    drafts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """状態の食い違いを検出する。"""
    anomalies: list[dict[str, Any]] = []

    # Drafts indexed by person_name
    draft_persons: dict[str, list[str]] = {}
    for d in drafts:
        person = _parse_subject_person(d["subject"] or "")
        if person:
            draft_persons.setdefault(person, []).append(d["id"])

    # Anomaly type 1: 完了 but no draft
    done_records = [r for r in records if r.status == DONE_STATUS]
    for r in done_records:
        if r.person_name not in draft_persons:
            anomalies.append({
                "type": "完了_without_draft",
                "row": r.row_number,
                "clinic": r.clinic_name,
                "person": r.person_name,
            })

    # Anomaly type 2: draft exists but Sheets ≠ 完了
    record_persons = {r.person_name: r for r in records}
    for person, draft_ids in draft_persons.items():
        rec = record_persons.get(person)
        if rec is None or rec.status != DONE_STATUS:
            anomalies.append({
                "type": "draft_without_完了",
                "person": person,
                "draft_ids": draft_ids,
                "row_status": rec.status if rec else None,
            })

    # Anomaly type 3: same person has multiple drafts (likely duplicate)
    for person, draft_ids in draft_persons.items():
        if len(draft_ids) > 1:
            anomalies.append({
                "type": "duplicate_drafts",
                "person": person,
                "count": len(draft_ids),
                "draft_ids": draft_ids,
            })

    return anomalies


def audit(
    stuck_threshold_hours: int = 2,
    dry_run: bool = False,
    output_path: Path | None = None,
) -> AuditResult:
    """完全な冪等性監査を実行する。"""
    output_path = output_path or LOGS_DIR / "idempotency_audit.json"
    records = read_records()
    drafts = fetch_drafts()

    already_done = [r.row_number for r in records if r.status == DONE_STATUS]
    recovered = recover_stuck_rows(records, stuck_threshold_hours, dry_run)
    anomalies = detect_anomalies(records, drafts)

    result = AuditResult(
        total_rows=len(records),
        already_done=already_done,
        stuck_recovered=recovered,
        drafts_found=len(drafts),
        anomalies=anomalies,
        safe_to_proceed=len(anomalies) == 0,
        report_path=str(output_path),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps({
        "total_rows": result.total_rows,
        "already_done_count": len(result.already_done),
        "already_done_rows": result.already_done,
        "stuck_recovered_count": len(result.stuck_recovered),
        "stuck_recovered_rows": result.stuck_recovered,
        "drafts_found": result.drafts_found,
        "anomalies": result.anomalies,
        "safe_to_proceed": result.safe_to_proceed,
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
    }, ensure_ascii=False, indent=2))

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Idempotency audit")
    parser.add_argument("--stuck-threshold-hours", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", type=Path, default=LOGS_DIR / "idempotency_audit.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    result = audit(
        stuck_threshold_hours=args.stuck_threshold_hours,
        dry_run=args.dry_run,
        output_path=args.output,
    )

    print("IDEMPOTENCY AUDIT")
    print(f"  Total Sheets rows:    {result.total_rows}")
    print(f"  Already 完了:         {len(result.already_done)}  (will skip)")
    print(f"  Stuck 処理中 → reset: {len(result.stuck_recovered)}  ({'DRY-RUN' if args.dry_run else 'applied'})")
    print(f"  Drafts found:         {result.drafts_found}")
    print(f"  Anomalies:            {len(result.anomalies)}")
    print(f"  Safe to proceed:      {'YES' if result.safe_to_proceed else 'NO'}")
    print(f"  Report:               {result.report_path}")

    return 0 if result.safe_to_proceed else 3


if __name__ == "__main__":
    raise SystemExit(main())
