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

### P-015: 重複検知は無条件で有効にし、bypass オプションを足さない（再処理は状態の手動修正で行う）
入力フォルダに PDF を継続追加して再実行する運用では、出力一覧シートに既存の管理番号（NNN-NN-N）を持つ PDF を毎回スキップする増分処理が必要になる。このとき「強制再実行（force_reprocess）」のような重複チェックを丸ごと無効化するフラグを足したくなるが、それはコストのかかる download / Claude API 呼び出しを全件で再実行させる足元の地雷であり、CLI フラグ・workflow input・フォーム質問・Apps Script payload と複数レイヤーに分岐を波及させる。重複検知の真の source of truth は「出力一覧シートに行があるか」であって、コードのフラグではない。**Rule**: 重複検知は無条件で有効にする（bypass パスを設けない）。再処理が必要な場合は、出力シートの該当行を手動削除すれば、その管理番号は「未処理」扱いに戻り次回実行で再処理される。状態（シートの行）を直せば挙動が変わる設計にしておけば、コード側に分岐を増やさずに済む。判定は download / API 呼び出しの前に行い、無駄なコストを発生させないこと。

### P-016: 「処理せず通すだけ」の経路はパイプラインの第一級分岐として明示せよ
入力の一部（添付資料 PDF）は AI 処理を一切せず出力へコピーするだけ。これを
メインループ内の if 分岐で「特別扱い」すると、抽出・コメント生成・結合の
各ステップに「これは添付資料だから飛ばす」という条件が散らばり、見落としと
責務の混線を生む。**Rule**: 「通すだけ」のデータは、入力を早期（ファイル名等の
安価な判定）に分類して別経路に振り分ける。メイン経路と passthrough 経路を
それぞれ単純に保ち、両者が合流するのは出力（Drive / シート）だけにする。
passthrough 対象も「処理した」記録は残す（出力一覧シートに種別付きで記録）。

### P-017: 外部 API 呼び出しは一過性エラーへのリトライを最初から組み込む
本番実行中、Google Sheets API が 503（The service is currently unavailable）を
1 回返しただけでワークフロー全体がクラッシュした。Google / 外部 API は 5xx・
429 を一定確率で必ず返す前提で設計すべきで、「1 回叩いて 1 回成功する」ことを
仮定したコードは長時間バッチで必ず破綻する。**Rule**: 外部 API のクライアントは、
一過性エラー（5xx / 429 / タイムアウト）に対する指数バックオフ・リトライを
最初から組み込む。`googleapiclient` なら `execute(num_retries=N)` /
`next_chunk(num_retries=N)`、Anthropic SDK なら組み込みリトライを使う。
リトライ対象は一過性エラーのみ（401/403/404 のような恒久エラーは即座に
失敗させ、無駄な再試行をしない）。

### P-018: 表示用フォルダ名に識別子を前置するときは「比較は正規化形・保存は元表記」を貫く
医院フォルダ名の先頭に医院番号（管理番号の先頭セグメント）を付与した
（`001_三浦歯科医院`）。識別子（医院番号）は管理番号から機械抽出した
安定値、医院名は AI 抽出値。両者を結合したフォルダ名をキーにすると、AI 抽出
側の表記揺れ（P-009）がそのまま重複フォルダを生む。**Rule**: 表示用の複合
フォルダ名（識別子 + 名前）を作るときも、フォルダの検索・再利用は
`find_or_create_folder` の正規化マッチング（P-009）に委ねる。識別子部分は
機械抽出で安定しているが、名前部分は揺れる前提で設計する。

### P-019: 識別子付き複合フォルダ名は識別子部分でのみマッチさせよ
医院フォルダ名を `<医院番号>_<医院名>` 形式にしたとき、フォルダの照合に
名前全体を使うと、AI 抽出側の医院名表記揺れ（`三浦歯科医院` vs `三浦歯科`
vs `医療法人三浦歯科`）で「同じ医院番号の別フォルダ」が量産される。
NFKC + 空白除去の正規化（P-009）は語そのものの違いを吸収できない。
**Rule**: 識別子と表示名を結合したフォルダ名（例 `<ID>_<表示名>`）の
存在チェックは、**識別子部分でのみ前方一致** で行う。表示名部分は重複
作成の判定材料にしない。新規作成時のフォルダ名には表示名を含めて
良いが、再利用判定では表示名を無視する。識別子が空の場合のみ、
表示名ベースのマッチング（P-009）にフォールバックする。同じ識別子で
複数のフォルダが既存する（このルール導入前にできた重複）場合は、
ID 昇順で決定論的に 1 つ選ぶ + 警告ログに重複一覧を出して手動統合を促す
（自動統合・自動リネームは破壊的変更となるため行わない）。実装例:
`src/drive_client.find_or_create_clinic_folder`。

### P-020: 副作用は本処理の冪等キーに従わせる（独自の重複チェックを足さない）
Gmail 下書き作成を本処理フロー（main / batch_main の Step4）に組み込む際、
「下書きが既にあるか」を独自にチェックしてリトライ・スキップする機構を
作るのは過剰設計になる。下書きは PDF 処理の **副作用** であり、PDF 処理
自体が管理番号で冪等（P-015）になっている以上、副作用も自動的に冪等になる
（PDF 処理がスキップされれば副作用もスキップされる）。逆に、副作用側で
独自の重複検知（draft の subject 検索など）を入れると、(a) 管理番号デデュープ
との二重防壁になりロジックが分散する、(b) 管理番号デデュープが効かない
ケース（管理番号なし PDF）でも副作用だけ動いて整合性が崩れる、(c) ユーザー
が再処理したいとき、出力シート行の削除に加えて draft の削除も必要になり
運用が複雑化する。**Rule**: 副作用（メール下書き / Slack 通知 / Webhook
コール等）は本処理の冪等キーに従わせる。本処理がスキップされたら副作用も
動かない、本処理が再実行されたら副作用も再実行される、という関係を保つ。
副作用側に独自のリトライ・重複チェックを足したくなったら、まず本処理の
冪等キーで十分かを確認する。後からメールアドレスを記入しても、その管理
番号が既に処理済みなら下書きは再作成されない（運用上は問題なし＝再処理
が必要なら出力シートの該当行を手動削除する、と説明しておく）。副作用の
失敗は警告ログだけ出して処理を続行する（fail-soft）。実装例:
`src/main._create_gmail_draft` / `src/batch_main._create_gmail_draft_safe`。

## Session Log
- **2026-03-16**: Project initialized with workflow orchestration architecture.
- **2026-05-01**: Added 5-agent team and 3 deterministic check scripts to handle 1000+ PDF scale. Each agent owns one of the failure modes in P-001 through P-005. See `tasks/todo.md` Phase 6 for the standard operating sequence.
- **2026-05-03**: Diagnosed `storageQuotaExceeded` regression after PR #8. Root cause was that service accounts cannot own files in My Drive (quota = 0). Fix: route Drive writes through OAuth user token (`GOOGLE_OAUTH_TOKEN_JSON`, falls back to legacy `GMAIL_TOKEN_JSON`). Sheets writes left on service-account auth (no quota issue there).
- **2026-05-17**: QA キャンペーン Phase 2 で発見された 3 バグを修正（HIGH: Drive ページング漏れ / MEDIUM: ファイル名 255 バイト超過 / LOW: `.pdf.pdf` 二重拡張子）。 P-010 / P-011 / P-012 を追加。横断 grep で同根の他箇所は検出されず（list_pdfs は既に正しいループ実装、ファイル名生成箇所は make_output_filename のみ）。テスト 261 → 265 件 / skip 3 → 0 件。
- **2026-05-17**: フォルダ自動検出モードを追加（`src/discover.py` + `--target-folder` 引数）。Convention over Configuration の方針で、Drive のサブフォルダを作るだけで新セミナーに対応可能。必要な Secret は `DRIVE_INPUT_ROOT` / `DRIVE_OUTPUT_ROOT` の 2 つだけで、新セミナー追加時の YAML/Secret/workflow 編集が不要に。既存の `--profile` モードは完全に後方互換維持。P-013 を追加。テスト 265 → 309 件（discover.py 単体 20 件、main/batch_main 拡張 9 件、integration smoke 6 件、profile mode regression 3 件、その他境界 6 件）。
- **2026-05-21**: 管理番号を自動採番からファイル名抽出に変更。実践事例 PDF のファイル名先頭に既存の管理番号（`NNN-NN-N` 形式）が埋め込まれていたため、自動採番（二重管理になる）を廃止し `src/utils.extract_management_number` で抽出する方式に。dead code（`sheets_client.get_max_management_number` / 全 `management_number_prefix` フィールド / `profiles/*.yaml` の prefix 行）を完全削除。全モード（`--profile` / `--target-folder`）共通でファイル名抽出。抽出不能ファイルは空文字列 + `logger.warning`。P-014 を追加。テスト 309 → 302 件（dead code テスト約 26 件削除、`extract_management_number` 等 19 件追加）。
- **2026-05-21**: 増分処理（管理番号での重複検知）を追加。出力一覧シートに既存の管理番号を持つ PDF は download / Claude API 呼び出しの前にスキップし、新規 PDF のみ処理する。仕様確定の際に「強制再実行（`force_reprocess` / `--force-reprocess`）」オプションは不要と判断し、一度実装しかけた `force_reprocess` 経路（`main.py` / `batch_main.py` の引数・argparse フラグ・分岐）を完全撤去。重複検知は無条件で有効とし、再処理は出力シートの該当行を手動削除して行う運用に統一。管理番号なし PDF は重複検知不可のためスキップ（fail-loud）。P-015 を追加。
- **2026-05-21**: 添付資料ファイルのパススルー処理を追加。ファイル名に「【添付資料】」を含む PDF は実践事例の補足資料であり、AI 処理（テキスト抽出 / Claude API / コメントページ生成 / PDF 結合）を一切せず、同じ管理番号（`NNN-NN-N`）のメイン実践事例 PDF と同じ `<医院名>/<個人名>/` フォルダへ元ファイル名のままコピーする。入力をファイル名で「メイン」「添付資料」に早期分類し（`utils.is_attachment_filename`）、メイン処理ループで管理番号 → `(医院名, 個人名)` の対応表を構築、添付資料はその表を引いてコピー + 出力一覧シートに「【添付資料】<元名>」で記録。Batch モードでは添付資料を Claude API に投げず、`batch_attachments.json`（`batch_prep.json` とは別ファイル）で Step1→Step4 に引き継ぎ、Step4 でメインと同じフォルダへコピー。重複判定セットは実行開始時の 1 スナップショットで、メイン処理が同一実行内の添付資料判定に影響しないようにした。メイン不在の添付資料は警告つきスキップ。P-016 を追加。テスト 319 → 342 件（`is_attachment_filename` 8 件、通常モード 7 件、Batch モード 6 件、integration smoke 2 件）。
- **2026-05-21**: Google Sheets/Drive API に一過性エラーの自動リトライを追加。本番のフォルダ自動検出モード実行中、`sheets_client.get_processed_management_numbers` 内の `spreadsheets().get(...).execute()` が Sheets API の 503（The service is currently unavailable）でクラッシュした。RCA: Google API クライアント（`sheets_client.py` / `drive_client.py` / `discover.py`）はリトライ機構を持たず、5xx・429 が 1 回起きるとワークフロー全体が落ちる。修正: `config.py` に `GOOGLE_API_NUM_RETRIES = 5` を定義し、grep で洗い出した全 14 件の `.execute()`（sheets 9・drive 4・discover 1）と `download_pdf` の `next_chunk` 1 件に `num_retries` を付与。`googleapiclient` が 5xx/429 を指数バックオフ + ジッターで自動再試行する（4xx の恒久エラーはリトライしない正しい挙動）。P-017 を追加。テスト 342 → 350 件（リトライ引数検証 8 件追加）。
- **2026-05-21**: 医院フォルダ名に医院番号を前置 + 医院フォルダURLシートを追加。(1) 医院フォルダ名を `<医院番号>_<医院名>`（例 `001_三浦歯科医院`）に変更。医院番号は管理番号 `NNN-NN-N` の先頭セグメント（`utils.extract_clinic_number`）。管理番号の先頭セグメントが 3〜5 桁可変になったため `extract_management_number` の正規表現を `^\d{3}-\d{2}-\d` → `^\d{3,5}-\d{2}-\d` に拡張（4〜5 桁医院番号の PDF がスキップされていた）。(2) 医院ごとに 1 行、医院フォルダ URL を `<出力シート名>_医院` シート（3 列: 医院番号 / 医院名 / 医院フォルダURL）へ記録。`sheets_client` に `get_recorded_clinic_numbers` / `append_clinic_folder_record` を追加し、`_ensure_output_sheet` をヘッダー長から列数を決める汎用ヘルパー `_ensure_sheet_with_header` に一般化。重複記録防止は実行開始時のスナップショット + in-memory set。`drive_client.upload_pdf_to_clinic_person` の戻り値に `clinic_folder_id` を追加。出力一覧シート（6 列）の医院名列は AI 抽出値そのまま（医院番号なし）を維持。添付資料もメインと同じ医院番号付きフォルダへコピー。P-018 を追加。テスト 350 → 390 件（`extract_clinic_number` 11 件、`extract_management_number` 桁数拡張 2 件、sheets 22 件、drive 1 件、main 5 件、batch_main 5 件、integration smoke 3 件 ほか）。既存の番号なしフォルダは孤立する（自動移行はしない）。
- **2026-05-25**: 医院フォルダの識別を医院番号ベースに変更（名前表記揺れで重複作成しない）。PR #33 で医院フォルダ名を `<医院番号>_<医院名>` 形式にしたが、フォルダの検索・再利用にフォルダ名全体を使っており、AI が同じ医院でも医院名を違う表記で抽出すると（`三浦歯科医院` vs `三浦歯科` vs `医療法人三浦歯科`）同じ医院番号で別フォルダが量産されていた。P-009 の正規化（NFKC＋空白除去）は語そのものの違い（接尾語の有無）を吸収できない。`drive_client.find_or_create_clinic_folder` を新規追加し、医院フォルダの **識別キーを医院番号のみ**（フォルダ名の `<医院番号>_` プレフィックス前方一致）に変更。既存フォルダ再利用時は医院名部分が違っても **リネームしない**。同じ医院番号で複数フォルダが既存する場合（本修正前にできた重複）→ フォルダID昇順で決定論的に1つ選ぶ + 警告ログに重複ID一覧を出し手動統合を促す。医院番号が空（管理番号なし）の PDF は元の名前ベース照合にフォールバック。`drive_client.upload_pdf_to_clinic_person` のシグネチャを `clinic_number` + `clinic_name` の別引数に変更。main / batch_main の呼び出し側を、医院フォルダ名 pre-build (`f"{n}_{name}"`) から `clinic_number` / `clinic_name` を分離して渡すように変更。case_map (添付資料パススルー対応表) も `(医院フォルダ名, 医院名, 個人名)` から `(医院番号, 医院名, 個人名)` に変更し、添付資料アップロード時に同じ `find_or_create_clinic_folder` 経由でメインと同じ医院フォルダへ合流させる。P-019 を追加。テスト 390 → 401 件。
- **2026-05-25**: Gmail 下書きの本処理フロー組み込み。`src/gmail_client.create_draft` は実装済みだったが main / batch_main から呼ばれていなかった（README §391 で「v2 では未使用」と明記済み）。設計: スプレッドシートに新規タブ `メールアドレス一覧`（5列: 医院番号 / 医院名 / 個人名 / 個人メール / 医院メール）を追加し、`sheets_client.read_email_records` で読み込み、`lookup_email` で `(医院番号, 個人名)` 完全一致 → 個人メール優先で TO 決定、医院メールは個人メールあり時のみ CC（無いとき TO に降格）、完全一致なしのとき同じ医院番号の他の行から医院メールを 1 つ拾うフォールバック、すべて空ならスキップ + 警告。`create_draft` に `cc_email` 引数を追加。`ProfileConfig` / `RunConfig` に `email_sheet_name` を追加（YAML 省略可、既定値は `EMAIL_SHEET_NAME` = `メールアドレス一覧`）。`email_records` はメインループ開始前に 1 回だけ読み込み、メイン経路 + 添付資料経路で共有（API 呼び出しを 1 回読みで済ます）。下書きは PDF アップロード成功後・シート追記後の位置に組み込み、例外は `logger.error` でログ出して処理続行（fail-soft）。下書き重複作成防止は管理番号デデュープ（P-015）に依存し、独自リトライ・重複チェックは入れない（P-020）。テスト 401 → 436 件（gmail_client 6 件 / sheets_client 16 件 / main 6 件 / batch_main 5 件 / integration smoke 2 件）。P-020 を追加。
