# AIComment - Lessons Learned

## Patterns & Rules
_Updated after corrections to prevent repeated mistakes._

### P-001: Silent skip is worse than loud failure
At 1000+ scale, `pdf_reader.extract_text()` returning `""` for unreadable PDFs causes the upstream pipeline to silently increment `stats["skip"]` without surfacing which PDFs failed. **Rule**: every component that can fail to produce output must classify the failure mode (not just return empty) and emit it to a manifest, not just a log line. See `scripts/triage_pdfs.py` for the canonical pattern.

### P-002: Re-runs must be idempotent before they can be safe
Gmail drafts are visible to humans; sending duplicates erodes trust. The Sheets `状態` column is the only durability bridge across runs. **Rule**: any code path that creates external side effects (Gmail drafts, Sheets writes) must be preceded by an audit step that reads the current state. See `scripts/audit_idempotency.py`.

### P-003: "完了" without observable output is a bug, not a state
If Sheets says `完了` but no Gmail draft exists for that person, the pipeline succeeded in the wrong layer. **Rule**: status updates and side-effect emissions must be transactionally co-located, or compensated by a reconciliation pass. The reconciliation pass is `idempotency-guardian`.

### P-004: Sample, don't trust
`pypdf` and `reportlab` both return success codes for outputs that humans would reject (blank page, missing comment, wrong font). **Rule**: any pipeline that produces customer-facing artifacts must end with a sampling validator that round-trips the output through the same readers customers use. See `scripts/verify_output.py`.

### P-005: Budget is a first-class limit
Without a budget gate, a misconfigured prompt + 1000 PDFs can produce $250+ of unintended spend. **Rule**: cost estimation must precede every large batch submission, and the kill switch must halt (not throttle silently) at the configured budget.

### P-006: Specialized agents own one failure mode each
The five-agent team in `.claude/agents/` each owns exactly one failure class from this lessons file. Adding a sixth agent should follow the same discipline: identify a distinct failure mode in production, not a hypothetical concern.

### P-007: API capability flags ≠ resource ownership
PR #8 added `supportsAllDrives=True` to every Drive API call assuming that would resolve `storageQuotaExceeded`. It did not. The flag only allows the API to *interact with* shared drives; it does not change the *ownership* of newly-created files. Service accounts always own files they upload, and have 0 GB quota — so uploads to My Drive folders fail even with the flag set. **Rule**: when an API error mentions "quota" or "ownership", trace which principal actually owns the resource, not which permissions the principal has been granted. The fix here was to switch Drive writes to OAuth user delegation so files are owned by the user (who has quota), not the service account.

## Session Log
- **2026-03-16**: Project initialized with workflow orchestration architecture.
- **2026-05-01**: Added 5-agent team and 3 deterministic check scripts to handle 1000+ PDF scale. Each agent owns one of the failure modes in P-001 through P-005. See `tasks/todo.md` Phase 6 for the standard operating sequence.
- **2026-05-03**: Diagnosed `storageQuotaExceeded` regression after PR #8. Root cause was that service accounts cannot own files in My Drive (quota = 0). Fix: route Drive writes through OAuth user token (`GOOGLE_OAUTH_TOKEN_JSON`, falls back to legacy `GMAIL_TOKEN_JSON`). Sheets writes left on service-account auth (no quota issue there).
