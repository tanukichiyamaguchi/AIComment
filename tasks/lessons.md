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

### P-008: クラウド完結が前提（ローカルPCは存在しないものとして設計せよ）
ユーザーはローカルPCでの実行を一切しない。OAuthのようにブラウザ認証が必要な処理でも、Codespaces / Cloud Shell / GitHub Actions の範囲で完結する手段（手動URL貼付フロー、デバイスフロー、Webクライアント型OAuth + Codespaces転送URL登録など）を最初から検討すること。**Rule**: 「ローカルでこうしてください」という回答は、このプロジェクトでは誤りである。提案前に「これはクラウド環境で動くか？」を必ず確認する。クラウド環境で動かない場合は、動くようにスクリプト/設定を修正することが正しい解決策であり、ローカル実行への退避は禁止。

### P-009: 同一性の判定は表示と分離せよ（外部ストレージでの重複防止）
`drive_client.find_or_create_folder` は完全一致でフォルダを検索していたため、AIが抽出した医院名に軽微な表記揺れ（「医療法人 かがやき歯科」と「医療法人かがやき歯科」など）があると別フォルダが作成され、Drive 上で重複が発生した。LLM 生成の自由テキストをキー（フォルダ名／一意性判定子）として使うとき、**表示用の文字列と同一性判定用の文字列は別物として扱うべき**。**Rule**: 外部ストレージ（Drive・Sheets・DB等）でユーザー由来文字列を一意キーに使う場合、必ず正規化関数（NFKC＋空白除去など）を経由した形で比較すること。新規作成時は元の表記を保持して保存する（表示優先）。比較は正規化形・保存は元表記、という分離が壊れると重複が無声で増殖する。

### P-010: ページング API は常に nextPageToken をループせよ（「最初の N 件」で打ち切らない）
`drive_client.find_or_create_folder` は `pageSize=1000` で `files().list()` を 1 回だけ呼んでおり、親フォルダに 1001 件以上のフォルダがある場合、2 ページ目以降にある既存フォルダを見落として重複作成していた（QA Phase 2 で発覚、HIGH severity）。Drive・Gmail・Sheets を含む Google API は仕様上 1 ページ最大 ~1000 件しか返さず、「pageSize を上げて 1 回で取り切る」という最適化は将来的に必ず破綻する。**Rule**: 「ある／無い」を判定する全ての list 系 API 呼び出しは、`nextPageToken` を辿るループとして実装する（途中で一致が見つかった時点で早期 break するのは可）。「N 件以下と決め打って 1 回で取得」する設計は禁止。`list_pdfs` のループパターンを正規実装として参照すること。

### P-011: ファイル名生成は OS 由来の上限を生成側で必ず保証せよ（sanitize ≠ 長さ制御）
`pdf_merger.make_output_filename` は `_sanitize_filename` で危険文字を除去するだけで、3 セクションを `f"{a}＿{b}＿{c}.pdf"` で結合した結果が 255 バイトを超えても通してしまっていた。ext4 / NTFS / FAT すべて 255 バイトが上限であり、本番で「File name too long」エラーになる（QA Phase 2 で発覚、MEDIUM severity）。`sanitize` 関数の責務は「危険な文字を除く」のみで、「長さを満たす」ではないことを常に意識すべき。**Rule**: ファイル名・URL パスセグメント・DB 列など、外部システムに渡る文字列を生成する関数は、各セクションを sanitize した後に、最終結合形が外部システムの上限（バイト長 or 文字長）以内になることを **assertion で不変条件として固定** する。文字数（`len(str)`）ではなく **UTF-8 バイト長**（`len(str.encode('utf-8'))`) で測ること（日本語は 1 文字 3 バイト）。各セクションの予算配分は均等割（`budget // N`）から始めて、余ったバイトを末尾セクション（title など最も可変なもの）に回す。

### P-012: 拡張子は単一 source of truth から付与せよ（入力に既に含まれていないか正規化する）
`make_output_filename` は `f"{...}.pdf"` で常に `.pdf` を付与していたが、AI 抽出した `sample_title` に既に `.pdf` が含まれているケース（元ファイル名をそのまま title に転記、Claude 出力が誤って拡張子を含む等）で `x.pdf.pdf` の二重拡張子になっていた（QA Phase 2 で発覚、LOW severity）。「拡張子を付ける／付けない」が複数箇所で判断されると必ず食い違う。**Rule**: ファイル名構築関数は「拡張子を付与する唯一の場所」となるべきで、入力に既に拡張子が含まれている可能性を最終結合前にチェック・除去する（`if name.lower().endswith(".pdf"): name = name[:-4]`）。大小区別なしの比較（`.PDF` `.Pdf` `.pDF` も同一視）を忘れない。同原則は `.tar.gz` のような多段拡張子・`http://` のような URL スキーマ・`@` のような sentinel prefix にも適用する。

### P-013: Convention over Configuration を優先せよ（設定追加の頻度が「新規データ種別ごと」になる設計は破綻する）
プロファイル YAML + GitHub Secrets を新セミナー追加のたびに増やす設計は、種別が増える前提の運用ではコスト青天井になる（新セミナー 1 つにつき YAML 1 ファイル + Secret 2 個 + workflow YAML 編集 + Apps Script の `ALLOWED_PROFILES` 編集 = 5 箇所の同期）。「設定で表現する」より「観測可能な状態（Drive のフォルダ命名）」から派生させる方が、ユーザー側の認知負荷も実装側のメンテナンス負荷も低い。**Rule**: 設定ファイル / Secret 追加の頻度が「新規データ種別ごと」になる設計が見えた時点で、auto-discovery（INPUT_ROOT 配下のサブフォルダを列挙して自動派生する等）への切替を検討する。後方互換のため既存の明示的設定モードは温存し、新方式は **追加機能** として並列に動かす（既存ユーザーの環境を一切壊さない）。表記揺れ吸収（P-009）と pageToken ループ（P-010）は auto-discovery の query にも同じく必須で、フォルダ重複作成 / 1000 件超漏れの両方を防ぐこと。導入実例: `src/discover.py` + `--target-folder` 引数（PR claude/folder-auto-discovery）。

### P-014: 識別子は「生成」より「抽出」を優先せよ（既存の外部識別子がある場合）
管理番号を自動採番していたが、実際には実践事例 PDF のファイル名先頭に既存の管理番号（NNN-NN-N 形式）が埋め込まれていた。システムが独自に番号を生成すると、現場の既存台帳と二重管理になり突合不能になる。**Rule**: 一意識別子が必要なとき、まず「入力データ内に既存の識別子がないか」を確認する。あるならそれを single source of truth として抽出する。生成は最後の手段。抽出時は正規表現でフォーマットを保証し、合致しないデータは警告とともに可視化する（サイレントに代替値を埋めない）。

## Session Log
- **2026-03-16**: Project initialized with workflow orchestration architecture.
- **2026-05-01**: Added 5-agent team and 3 deterministic check scripts to handle 1000+ PDF scale. Each agent owns one of the failure modes in P-001 through P-005. See `tasks/todo.md` Phase 6 for the standard operating sequence.
- **2026-05-03**: Diagnosed `storageQuotaExceeded` regression after PR #8. Root cause was that service accounts cannot own files in My Drive (quota = 0). Fix: route Drive writes through OAuth user token (`GOOGLE_OAUTH_TOKEN_JSON`, falls back to legacy `GMAIL_TOKEN_JSON`). Sheets writes left on service-account auth (no quota issue there).
- **2026-05-17**: QA キャンペーン Phase 2 で発見された 3 バグを修正（HIGH: Drive ページング漏れ / MEDIUM: ファイル名 255 バイト超過 / LOW: `.pdf.pdf` 二重拡張子）。 P-010 / P-011 / P-012 を追加。横断 grep で同根の他箇所は検出されず（list_pdfs は既に正しいループ実装、ファイル名生成箇所は make_output_filename のみ）。テスト 261 → 265 件 / skip 3 → 0 件。
- **2026-05-17**: フォルダ自動検出モードを追加（`src/discover.py` + `--target-folder` 引数）。Convention over Configuration の方針で、Drive のサブフォルダを作るだけで新セミナーに対応可能。必要な Secret は `DRIVE_INPUT_ROOT` / `DRIVE_OUTPUT_ROOT` の 2 つだけで、新セミナー追加時の YAML/Secret/workflow 編集が不要に。既存の `--profile` モードは完全に後方互換維持。P-013 を追加。テスト 265 → 309 件（discover.py 単体 20 件、main/batch_main 拡張 9 件、integration smoke 6 件、profile mode regression 3 件、その他境界 6 件）。
- **2026-05-21**: 管理番号を自動採番からファイル名抽出に変更。実践事例 PDF のファイル名先頭に既存の管理番号（`NNN-NN-N` 形式）が埋め込まれていたため、自動採番（二重管理になる）を廃止し `src/utils.extract_management_number` で抽出する方式に。dead code（`sheets_client.get_max_management_number` / 全 `management_number_prefix` フィールド / `profiles/*.yaml` の prefix 行）を完全削除。全モード（`--profile` / `--target-folder`）共通でファイル名抽出。抽出不能ファイルは空文字列 + `logger.warning`。P-014 を追加。テスト 309 → 302 件（dead code テスト約 26 件削除、`extract_management_number` 等 19 件追加）。
