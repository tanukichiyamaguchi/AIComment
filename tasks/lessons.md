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

### P-021: 副作用 (Gmail 下書き) と表示用標準化 (医院名) を同じマスターシートに統合する
PR #35 で「メールアドレス一覧シート」（Gmail 下書きの TO/CC 用）を導入した直後、
ユーザーから「既に運用している管理シート（管理番号 / 医院名 / 参加者名 / 会場 /
メールアドレス）を 1 つ使い、医院フォルダの命名にも使いたい」という要望が来た。
別々のシートで lookup する（複数シートを保守する）より、**ユーザーが既に運用
している 1 シートを唯一の真実とする** ほうが運用が単純になる。
**Rule**: 似た用途の lookup（表示用標準化 / 副作用の送信先 など）を別シートに
分けたくなったら、まず「ユーザーが運用している 1 シート」に統合できないかを
検討する。1 シート 2 用途は (a) ユーザーの保守負担が 1 か所に集約される、(b)
データの整合性（同じ医院番号で別シートの医院名が違う等）の心配がない、(c)
新セミナーごとの初期設定が「1 シート + 1 タブ名」だけになる、というメリットが
ある。lookup ミス時の fallback は **用途別に振る舞いを分ける**:
表示用（医院名）→ AI 抽出値で代用 + 警告ログ（処理は続ける）、
副作用（メール送信）→ 宛先空で下書き作成 + 警告ログ（fail-soft、PDF 処理は続ける）。
代用時は必ずログを出す（サイレント代用は事故の元）。実装例: `src/sheets_client.MasterRecord` /
`lookup_clinic_name` / `lookup_email_by_clinic_and_person`。

### P-023: 副作用（メール送信）の集約はループ末尾で 1 回行う設計にする
同じ個人に対する複数 PDF を 1 通のメール下書きに集約する要件のとき、ループ
内で「既存下書きを探して添付追加」する設計は (a) Gmail API の draft 検索
が必要、(b) 同期化が必要、(c) `get + update` のレース条件 と複雑度が
跳ねる。代わりに **ループ中は (email, person_name, pdf_path) を蓄積する
だけ** にして、**ループ完了後に email 単位でグルーピングして 1 回ずつ
``create_draft`` を呼ぶ** ほうがシンプルで一過性エラーへの耐性も高い。
**Rule**: 副作用の「集約」要件が出たときは、ループ内で外部状態を読み書き
してまとめるのではなく、ローカルバッファに蓄積してループ末で 1 回まとめる
パターンを優先する。一時ファイル（添付 PDF 等）はセッションスコープの
``mkdtemp`` で保持し、集約完了後に ``shutil.rmtree`` で一括削除する。
グループ化キーが存在しない項目（例: メール空）はキー無し扱いでそのまま
個別処理する。実装例: `src/main._create_grouped_drafts_for_run` /
`src/batch_main._create_grouped_drafts_for_batch`。

### P-022: 識別子の意味は「ファイル名上の形式」と「マスター上の概念」を独立に扱え
PR #36 で参加者マスターの A 列を 2 セグメント管理番号 (``xxx-yy``) で
設計したが、PDF ファイル名先頭の管理番号は 3 セグメント (``xxx-yy-z``) で、
両者を文字列マッチングする前提自体が誤りだった。ユーザーの説明上は同じ
「管理番号」だが、運用上の **概念が別物**（PDF 側は提出単位、マスター側は
個人単位）であり、文字列を直接比較する設計は将来必ず破綻する。
**Rule**: 入力データの識別子とマスターデータの識別子が形式違いで存在する
とき、両者が同じ概念かをユーザーに必ず確認する。「先頭 N 文字を切る」
「末尾セグメントを足す」といった文字列加工で繋ぐ設計はサイレント不一致の
温床になり、ヒット率 0% でも気づきにくい（テストはモックで通る、本番で
初めて「メール未登録」が出続けて顕在化する）。本来の解決策は、両者の
**意味的に独立な属性**（医院コード + 個人名 等）で突合すること。
個人名のような自由テキストは強めの正規化（NFKC + 全空白除去 + カナ統一）+
1 文字差ファジー一致（Levenshtein 距離 ≤ 1）で表記揺れを吸収する。
不一致時はサイレント失敗を避けるため宛先空でも下書きを作り、見落としと
誤発送の両方を防ぐ。実装例: `src/sheets_client.lookup_email_by_clinic_and_person`
/ `_normalize_person_name` / `_levenshtein_distance`。

### P-024: 「ラン全体で恒久的に失敗する条件」は per-item fail-soft と分離して即停止せよ（fail-fast）
本番 GHA ラン（164 件）で、途中で Anthropic のクレジット残高が尽き、88 件成功
後の残り 49 件が**全件** `BadRequestError`（`credit balance is too low`）になった。
当時の `main.run` / `batch_main.step4` は各 PDF の例外を一律
`except Exception: stats['error'] += 1; continue` で握りつぶしていたため、
「以降のどの API 呼び出しも必ず失敗する」恒久条件でも 49 回ぶん無駄に
API を叩き、エラーログを乱立させた。per-PDF の fail-soft（1 件固有の失敗で
全体を止めない）は正しいが、それは「リトライ/スキップすれば次は成功し得る」
一過性・item 固有エラーにのみ妥当で、残高切れ・認証(401)・権限(403)のような
**run-wide permanent failure** に適用すると「確実に失敗する処理を N 回繰り返す」
アンチパターンになる。**Rule**: 外部 API を item ループで叩くバッチ処理では、
捕捉した例外を「(a) その item 固有の失敗（→ ログ + continue）」「(b) ラン全体が
継続不能な恒久条件（→ 即停止）」に**必ず型・内容で分類**する。判定は
**例外の型**（`AuthenticationError`/`PermissionDeniedError`）と、汎用 400 の中の
**message 内容**（billing 文言 `credit balance` / `billing` / `purchase credits` 等を
含むか）で行う。billing と無関係な request-specific 400（プロンプト過大など）は
従来通り per-item fail-soft（ラン全体を止めない）。恒久条件は専用例外
（`comment_generator.PermanentRunFailureError`）に正規化し、(1) `generate_comment_with_metadata`
はリトライせず即送出（一過性リトライ tuple に混ぜない）、(2) 呼び出し側ループは
break して残り item を処理しない、(3) 成功済みの成果物 flush と一時ファイル
削除は済ませてから、(4) 例外を再送出して GHA ジョブを**非ゼロ終了**させる
（silent return は「未処理 49 件があるのにワークフローが緑」になり事故）。
注意: `except SomeError` の `SomeError` を**モジュール経由参照**
（`comment_generator.PermanentRunFailureError`）で書くと、テストで
`comment_generator` モジュール全体がモックされたとき `except <MagicMock>` で
`TypeError` になり既存テストを巻き込む。例外クラスは呼び出し側モジュールへ
**直接 import**（`from src.comment_generator import PermanentRunFailureError`）して、
`except` 節がモックに依存しないようにすること。これは堅牢化であり、49 件の
エラー自体の消去にはクレジット追加（請求側対応）が別途必要。実装例:
`src/comment_generator.is_permanent_run_failure` / `PermanentRunFailureError`
/ `permanent_failure_message`、`src/main.run` の `except PermanentRunFailureError`
+ break、Batch は `submit_batch` / `get_batch_status` / `get_batch_results` で
変換し `step2`/`step3` 経由で `run` 外へ伝播。

### P-025: 一過性リトライは「API 呼び出し 1 箇所」だけでなく「その上位ポーリングループ」にも入れよ（多層防御）
本番 GHA ラン（run_id=26811653746, branch=main, 3h22m）で、Batch API への
バッチ送信は成功 67 件投入したが、3 時間ポーリング後の最終 1 回で
`get_batch_status()` 内の `client.messages.batches.retrieve(batch_id)` が
`503 overloaded_error` (`API key validation is temporarily unavailable. Please retry.`)
を返した瞬間、リトライされず即送出され `step3_wait_and_get_results` の `while`
ループを抜けて Traceback。**3 時間ぶんの待機が水の泡**になった。原因は二段:
(a) PR #47 で `_RETRYABLE_API_ERRORS` + `_backoff_seconds` を導入したが、
    `generate_comment_with_metadata` 内でのみ使われており、Batch 系 3 関数
    （`submit_batch` / `get_batch_status` / `get_batch_results`）は
    PR #46 の **恒久エラー変換 except だけ** を持って一過性リトライがなかった。
(b) `step3_wait_and_get_results` のポーリングループ本体も `try/except` 無しで、
    `get_batch_status` が 1 度でも例外を出すと即終了する設計だった。
**Rule**: 長時間ポーリング系の処理では、API 呼び出し 1 箇所のリトライだけに
頼らず、**その上位のループ** にも同じ「一過性は continue / 恒久は即 raise」の
例外耐性を入れる（多層防御）。1 関数のリトライ上限を超えても、ループそのもの
は ``poll_interval`` 待って継続できる設計にしておけば、半日待った状態を 503
1 発で失わなくて済む。判定対象に `503 overloaded_error` も必ず含むこと
（OverloadedError は 529、`InternalServerError` は 5xx の汎用親、両方
`_RETRYABLE_API_ERRORS` に含まれている）。

また、こうした「途中で死ぬ」前提のバッチ処理では、再開可能な永続キー
（Anthropic の場合は `batch_id`、保持期間 29 日）を atomic write で必ず
ディスクへ書き、別 GHA 実行から `--step results --batch-id <id>` で再開できる
経路を残す。バッチ送信は既に課金確定済みなので、再送信すると同じ料金が
再度発生する（=「3 時間+再送 6 時間で結局 9 時間」になる）。実装例:
`src/comment_generator._call_with_retries`（Batch 系 3 関数共通の retry helper）、
`src/batch_main.step3_wait_and_get_results` のループ内 try/except、
`src/batch_main.step2_submit_batch` の `batch_id.txt` atomic write。

### P-028: 「安全のための分離」を再導入するときは、その分離を破る空状態を HARD FAIL で必ず可視化する
F-09（per-folder マスタータブ）は 2026-05-29 に「ユーザーの共有タブ運用と
食い違う」という理由で撤回したが、その後のセミナー数増加で「セミナーごとに
独立した参加者管理」要求が再浮上した。再導入時に同じ事故（タブ未準備のまま
実行 → 医院名フォルダが AI 抽出値・Gmail 下書きが全件宛先空）を防ぐため、
**タブ不在 / 0 件を WARNING で済ませず HARD FAIL（``MasterSheetEmptyError``）
で即停止** にした（自動検出モードは ``master_sheet_strict=True``、プロファイル
モードは ``False`` で後方互換）。**Rule**: 「安全のための分離」（per-seminar /
per-customer 等の物理分離）を導入・再導入するとき、分離先のリソースが
未準備のままで実行できてしまう経路は「サイレント空動作」の温床になる。
最初から HARD FAIL で気づかせる設計にする（WARNING ログは本番運用で
見落とされる、F-05/F-11 補強を経ても見落とされた実績がある）。Batch モードでは
**Anthropic API 投入前**（Step1）にガードして料金の無駄も同時に防ぐ。
Resume パス（``--step results``/``pdfs``）にも保険として同じガードを Step4 で
重ねる（多層防御、P-025 と同じ思想）。``master_sheet_strict`` は
``RunConfig`` のフィールドで、``ProfileConfig`` には無いので ``getattr(profile,
"master_sheet_strict", False)`` の fallback で両モード共存する（後方互換）。
HARD FAIL 時は出力一覧シートに「中止（参加者マスタータブ未準備）」マーカーを
fail-soft で 1 行追記してから例外を送出する（GHA を非ゼロ終了 + シートでも
停止が分かる）。実装例: `src/discover.RunConfig.from_discovered` の
``master_sheet_name=f"参加者マスター({ctx.target_folder_name})"`` /
``master_sheet_strict=True``、`src/run_common.MasterSheetEmptyError` /
``require_non_empty_master``、`src/main.run` と `src/batch_main.step1_prepare` /
``_process_results_and_create_pdfs`` の HARD FAIL 経路。

### P-029: `max_tokens` は「全フィールド合計の最悪ケース × 言語のトークン密度」で決める（特に可変長フィールドが最後にあるとき）
本番 PDF のコメントが文の途中（「…前頭葉がまだ育っていないから」）で
切れていた。RCA: 1 回の構造化出力（json_schema）で
``clinic_name / person_name / sample_title / comment`` の 4 フィールドを
まとめて生成しており、``CLAUDE_MAX_TOKENS = 1024`` が小さすぎた。``comment``
はスキーマの**最後**のフィールド（プロンプト指定で 200〜350 字）なので、
上限到達時に真っ先に切れる。日本語は 1 文字 ≈ 1〜1.5 トークンと重く、
医院名・氏名・長いタイトル + 350 字のコメントで容易に 1024 を超える。
さらに悪いことに、構造化出力の制約デコードは打ち切られても JSON を閉じる
ため ``_parse_extraction`` は成功し、**半端なコメントが "succeeded" 扱いで
そのまま PDF 化**されていた（``result.result.type`` しか見ておらず
``stop_reason`` を見ていない）。**Rule**: ``max_tokens`` は「出力する全
フィールドを合計した最悪ケースの文字数 × 対象言語のトークン密度」に
**数倍のヘッドルーム**を掛けて決める。可変長フィールド（自由記述コメント等）が
スキーマの最後にあると、上限到達時に最も重要な内容が黙って欠落する。
英語感覚の ``1024`` を日本語の長文生成に流用しない。今回は ``4096``（最悪
ケース ~900 トークンに対し ~4.5 倍）へ引き上げて、正常出力では上限に
当たらないようにした。実装: `src/config.py` の ``CLAUDE_MAX_TOKENS``
（``_build_extraction_request_params`` と ``create_batch_requests`` の
両方が単一定数を参照しているため 1 箇所の変更で通常・Batch 両モードに波及）。

### P-027: .gitignore からエントリを外す変更と `git add -A` を同時に行わない
Phase 17-A で `.gitignore` から `node_modules/` / `dist/` を外した直後に
`git add -A` でコミットしたところ、ディスク上に残っていた node_modules と
dist（約 50 万行）がコミットに混入した（amend で即修正）。ignore されていた
パスは「見えていないだけで存在する」。**Rule**: `.gitignore` のエントリを
削除するときは、(1) 先に該当パスの実体を `rm -rf` で削除（または意図的に
追跡開始するか判断）し、(2) `git status` で staged 対象を確認してから
コミットする。`git add -A` は ignore 解除直後の作業ツリーに対しては
「何が入るか」を必ず目視確認する。コミット直後の `git show --stat` で
意図したファイル数か検算する習慣も有効（今回 357 files changed で気づけた）。

## Session Log
- **2026-06-26**: コメント PDF の改行を「文脈の切れ目」で入るようにした。問題: コメントが 1 段落のべた書きで、PDF 描画側 (`_wrap_text`) は機械的な幅オーバー折り返ししかしていなかったため、句読点が行頭に来る（行頭禁則違反）/ 文脈の区切りが視覚的に分からない、という読みにくさがあった。文脈判定は描画側（reportlab）では原理的に不可能なので、**生成側（SYSTEM_PROMPT）に「文脈の切れ目で `\n` を挿入する」指示を追加**し、Claude に段落分けされた状態で返してもらう設計に。PDF 描画側は元々 `text.split("\n")` で段落を独立処理する仕組みだったため、プロンプトの追加だけで段落分けが反映される。落とし穴: プロンプト本文に `\n` と書くと Python 文字列リテラルで LF として解釈されて Claude には改行が届くだけになり「`\n` というリテラル文字を入れろ」という指示にならない → `\\n` でエスケープして Claude に文字 `\n` として見せる必要があった（リテラル文字列の二重解釈の罠）。同時に補助対応として `_wrap_text` に行頭禁則処理（「、」「。」「！」「？」「）」等 14 文字を frozenset で持ち、次行頭に来そうな場合は前行末尾にぶら下げ）を追加。テスト 610 → 617 件（行頭禁則 5 件 + 段落区切り 2 件 + 連続改行 1 件 + プロンプト指示の存在検証 1 件、ベースから -1 件は既存 placeholder 統合）、mypy clean。
- **2026-06-26**: テーマ別プロンプトの分岐を撤廃し、じっせん実践事例プロンプト（``SYSTEM_PROMPT``）に統一。ユーザー方針「じっせんのプロンプトを共通とし、その他に分岐する設定はなしにする」。``src/comment_generator.py`` から ``extract_theme`` / ``get_system_prompt`` / ``READING_SYSTEM_PROMPT`` / ``LIG_REPORT_SYSTEM_PROMPT`` / ``PARTNER_SYSTEM_PROMPT`` / ``TEAM_MTG_SYSTEM_PROMPT`` / ``_KNOWN_THEMES`` / ``_THEME_PROMPTS`` / ``_PRACTICE_PRAISE_HEAD/TAIL`` / 各テーマ別 ``_EXAMPLES`` を削除（約 100 行のプロンプト定数 + 50 行のロジック削減）。通常モード（``generate_comment_with_metadata``）と Batch モード（``create_batch_requests``）の両方で system プロンプトを ``SYSTEM_PROMPT`` 固定。「テーマ判定」ログも削除。テスト側は 6 テーマ判定クラス（``TestExtractTheme`` / ``TestGetSystemPrompt`` / ``TestReadingSystemPrompt`` / ``TestPracticePraisePrompts`` / ``TestExtractionRequestParamsUsesProvidedPrompt`` / ``TestCreateBatchRequestsPicksThemePerItem``）を削除し、「ファイル名に関わらず常に SYSTEM_PROMPT が使われる」検証 1 クラス（2 ケース）に置換。pytest 638 → 610 件 / mypy clean。Phase 17-E（プロンプト共通化の保留事項）も同時に解消（分岐自体を撤廃したので共通化問題が消滅）。
- **2026-06-24**: 本番コメントが文の途中で切れる事象を修正（P-029）。出力 PDF のコメントが「…前頭葉がまだ育っていないから」で途切れていた。RCA: 構造化出力 4 フィールド（clinic_name / person_name / sample_title / comment）を 1 回で生成する設計で ``CLAUDE_MAX_TOKENS = 1024`` が小さく、スキーマ最後の comment（200〜350 字）が日本語のトークン密度で上限に当たり切れていた。制約デコードが JSON を閉じるため "succeeded" のまま半端コメントが PDF 化されていた。レンダリング側（``pdf_creator`` の改ページ break）は、テキストがページ上部で終わり下に余白が残る＝ループが ``wrapped_lines`` を使い切って終了しており無関係と切り分け。対応はユーザー要望により「そもそも上限に当たらないようにする」一点に絞り、``CLAUDE_MAX_TOKENS`` を 1024 → 4096 へ引き上げ（``stop_reason == "max_tokens"`` ガードは付けない方針）。単一定数を通常モード（``_build_extraction_request_params``）と Batch モード（``create_batch_requests``）の両方が参照しているため 1 箇所の変更で両経路に波及。既存テストは max_tokens 値をハードコードしていないため 638 件維持・mypy clean。reporter: ユーザー（本番 PDF のスクショ）。
- **2026-06-24**: 参加者マスタータブをセミナーごとに分離 + 空タブ HARD FAIL（P-028）。target_folder モード（フォルダ自動検出）で ``RunConfig.from_discovered`` の ``master_sheet_name`` 派生を共有 ``参加者マスター`` から ``f"参加者マスター({target_folder_name})"`` に変更し（例: 入力フォルダ ``新人育成塾`` → タブ ``参加者マスター(新人育成塾)``）、セミナーごとに独立した参加者管理を実現。同時に ``RunConfig`` に ``master_sheet_strict: bool`` フィールドを追加し自動検出モードでは ``True``、プロファイルモードでは ``False`` で後方互換。``src/run_common.MasterSheetEmptyError`` + ``require_non_empty_master`` ヘルパーを新設し、target_folder モードでタブ不在 / 0 件のとき PDF 処理ループに入る前に HARD FAIL → 出力一覧シートに「中止（参加者マスタータブ未準備）」マーカーを fail-soft で追記 → 例外再送出で GHA 非ゼロ終了。Batch モードは Step1 開始時に Anthropic API 投入前のガードを入れ、Step4 にも resume パスの保険として重ねる（多層防御、P-025 同思想）。pytest 629 → 638 件（HARD FAIL 検証 4 件 + 後方互換検証 1 件 + run_common 単体 3 件 + smoke 修正 1 件）、mypy clean。F-09 撤回（2026-05-29）の理由「ユーザー運用と食い違う」は HARD FAIL で「気づけない事故」を排除することで解消。
- **2026-06-11**: Phase 17 一掃リファクタリング（A〜C）。(A) 初期スキャフォールド由来の TypeScript 一式（src/*.ts, tests/*.test.ts, tsconfig, package.json, CI の typescript-tests ジョブ）を削除。本番経路（python -m src.batch_main / src.main）からの参照ゼロ・実装はモックのみであることを調査で確定してから削除し、CLAUDE.md の Project Overview / Build & Test を Python 実態に更新。(B) 完全デッドコードの src/matcher.py + test_matcher.py を削除（本体コードから呼び出しゼロ、AI 抽出への置き換えで不要化済み）。(C) main / batch_main の二重実装（PDF 分類 / デデュープ / 医院名標準化 / 医院フォルダ記録 / 下書き蓄積・集約 / 完了マーカー / 添付資料パススルー、計 476 行）を src/run_common.py へ単一実装として集約。依存モジュールは注入方式にして既存テストのモジュール属性パッチを維持し、629 テスト無修正で全パス。外部挙動不変。P-027 を追加。
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
- **2026-05-26**: 参加者マスターシートに統合（医院名標準化 + Gmail 下書きの TO）。PR #35 で追加した「メールアドレス一覧」シート方式（5 列: 医院番号 / 医院名 / 個人名 / 個人メール / 医院メール）を廃止し、ユーザーが既に運用している「参加者マスター」シート（5 列: 管理番号 / 医院名 / 参加者名 / 申し込み会場 / メールアドレス）を **唯一の lookup ソース** に統合。理由: ユーザーが新セミナーごとに 1 シートを準備する運用に合わせて、(a) 医院名の表記統一（フォルダ名・出力シート列・PDF ヘッダーすべて同じ標準名）、(b) Gmail 下書きの TO ルックアップ、を同じシートで賄えるようにした。`sheets_client` の旧 `EmailRecord` / `read_email_records` / `lookup_email` を削除し、新 `MasterRecord` / `read_master_records` / `lookup_clinic_name` / `lookup_email_by_management_number` を追加。医院名 lookup は管理番号 prefix（`101-`）の **前方一致**、メール lookup は管理番号 **完全一致**。`gmail_client.create_draft` の `cc_email` 引数は将来再利用のため残すが、main / batch_main からは常に `None` を渡す（現運用では CC を使わない）。`ProfileConfig` / `RunConfig` の `email_sheet_name` を `master_sheet_name` に rename、`config.EMAIL_SHEET_NAME` を `MASTER_SHEET_NAME` に rename。lookup ミス時の fallback: 医院名は AI 抽出値で代用 + 警告ログ、メールは下書きスキップ + 警告ログ（fail-soft、PDF 処理は続行）。`master_records` はメイン経路 + 添付資料経路で共有（1 回読み）。`case_map` の医院名はマスター標準化済みの値を保持し、添付資料経路で再 lookup しない。テスト 436 → 440 件（sheets_client 16 件入替: EmailRecord 系 16 件削除 → MasterRecord 系 16 件追加 / main 8 件入替 / batch_main 7 件入替 / integration smoke 2 件入替）。`profiles/jissen_2024_q1.yaml` に `master_sheet_name` 指定例を追記、`README.md` §4-4 を全面書き換え。P-021 を追加。
- **2026-05-26**: Gmail 下書きが全件 TO 空になる事象を調査。原因は **コードではなく運用** だった。ログに `Sheets: 参加者マスター 0件を取得 (参加者マスター)` が大量出力されており、デフォルト `参加者マスター` タブが空（ヘッダーのみ自動作成された状態）のまま PDF 処理を実行していた。ユーザー意図はセミナーごとに **同じ「参加者マスター」タブの中身を差し替える** 運用で、`<セミナー名>_参加者マスター` のようなプレフィックス付き別タブを作る運用ではない（一時的にそういうタブを作っていたが、コードの自動連携がない以上プレフィックスは無意味と判断）。最終対応: `profiles/jissen_default.yaml` には `master_sheet_name` を **追加しない**（デフォルト `参加者マスター` を使う）。代わりに YAML コメントで「タブが空だと全件宛先空でフォールバック下書きになるので、実行前に行が入っているか確認すること」と明記。教訓: (1) 「auto-create on missing」は安全側に見えて設定漏れを **黙って吸収して空動作する** サイレント失敗を生む。`_ensure_*` 系で新規作成時に WARNING を出す改善余地あり（次回改修候補）。(2) コード変更前にユーザーの運用意図（タブ名運用 / 設定運用 / 自動連携、のどれを望むか）をまず確認すること。本件は最初に「マスター内容が空」可能性を提示すべきだった。
- **2026-05-29**: 1000-PDF 完走監査 → 重大欠陥 4 件 + 高リスク 3 件を修正（P-023）。並列 5 エージェント（batch-orchestrator / pdf-triage-officer / resource-cost-sentinel / idempotency-guardian / output-verifier）で 1000 件 Batch 完走耐性を監査し **NO_GO** verdict が出たため、以下を修正:

  **CB-1: `batch_prep.json` から `pdf_text` を捨てない**。旧仕様は「save_items から pdf_text を除外」していたため、別 GHA 実行から `--step submit` を呼ぶと Batch API に空 prompt が投げられて全件 garbage が返る本番事故が起こる構造だった。`_atomic_write_json` でアトミック保存し、Step1→Step2/Step3/Step4 を別 GHA 実行で繋げるシナリオに対応。

  **CB-2: step3 結果も `batch_results.json` に永続化 + run() で disk からの自動ロード**。submit / results / pdfs のいずれかを単独実行する場合、in-memory 状態が無くても `_load_items_from_disk` / `_load_results_from_disk` で復元される。これで 6h GHA timeout を回避するための「step 分割実行」が実装上初めて機能する。

  **CB-3: Step4 開始時に処理済み管理番号を再スナップショット**。`step4_processed = sheets_client.get_processed_management_numbers(...)` を Step4 冒頭で再取得し、各 item が既に Sheets に書かれている場合は Drive アップロード・Sheets 追記・下書き蓄積をスキップ。Step4 部分失敗からの再実行で Drive/Sheets/Gmail 重複が出ないようにした。`stats["skip_already_processed"]` を新設して可観測化。

  **H-2: Sheets API rate throttle**。`_throttle_sheets_write()` で 50 writes / 60 sec を能動的に強制（ハード上限 60/min の手前）。`append_output_record` / `append_clinic_folder_record` の直前で呼ぶ。1000 PDF 規模の Step4 で 429 quota_exceeded が連発して落ちる事故を予防。`GOOGLE_API_NUM_RETRIES=5` は一過性エラー対策であり、quota の自衛策ではない（P-017 補強）。

  **H-5: ログローテーション**。`logs/jissen_comment.log` が GB 級まで肥大化して artifact upload / Codespaces disk を食い潰す事故が観測されていた。`logging.handlers.RotatingFileHandler(maxBytes=100MB, backupCount=5)` に切り替え。`setup_logging()` の多重初期化防止（既存ハンドラ検知で early return）も同時に対応。

  **M-1: Drive 同名ファイル重複アップロード防止**。`drive_client.upload_pdf` でフォルダ内の同名ファイルを `_find_file_in_folder` で先に検索し、既存があれば再アップロードせず既存 ID/URL を返す。同名複数（過去の重複の名残）は ID 昇順の先頭を採用 + WARNING。Drive は同名・同所のファイル重複を許容するため、Step4 再実行で同じ PDF が複数アップロードされる構造的バグへの対処。

  **M-2: Step1 スキップ manifest の可視化**。`batch_step1_skips.json` を新設して、管理番号なし / 処理済み / 抽出失敗 / 取得エラーの 4 分類で失敗 PDF を file_name + 理由 + （あれば）エラーメッセージ込みで永続化。Step1 のサマリーログにも 4 分類のカウントを出す。1000 件中の silent skip が後追いできる。

  **GH-1: GitHub Actions concurrency**。`generate_comments.yml` に `concurrency.group` を追加し、同じ profile / target_folder への同時 2 実行を直列化（`cancel-in-progress: false` でキューに入る）。並行実行による Drive/Sheets/Gmail での同一 PDF 重複処理を防ぐ。

  テスト 477 → 507 件（22 件 P-022 用 + 8 件 P-023 用）。新規テスト内訳: Sheets throttle 3 / Drive 重複防止 2 / batch state 永続化 3。

  教訓: (1) **「副作用としてのリソース生成」は別 step 実行で破綻する**。Step1 が pdf_text を捨てたのは「ファイルサイズ削減」の善意だったが、step 分割の前提を破った。永続化は機能と粒度のセットで設計する。(2) **GHA 6h vs Batch 24h の不整合は構造的**。step 分割を実装上 functional にするのは前提条件。(3) **rate limit は retry でカバーできない領域がある**。GOOGLE_API_NUM_RETRIES は一過性エラー対策で、quota 自衛にはアクティブ throttle が要る。(4) **重複防止は per-resource で設計する**。Drive ファイル単位、Sheets 行単位、Gmail 下書き単位それぞれにベキ等キーが要る。今回は Drive と Sheets を Step4 冒頭の `processed` 再スナップショット + 同名検索でカバー、Gmail は管理番号デデュープに依存（既存設計を継承、改善余地あり）。
- **2026-05-26**: 包括的 QA キャンペーン → NO_GO 解消（P-022）。並列 3 エージェント（qa-orchestrator / profile-system-architect / integration-validator）で直近 PR #36/#37/#38 の master-sheet 統合を監査し、Critical 3 件 + High 2 件 + Medium 1 件を発見・修正。修正内容: **(F-01) 短い個人名のファジー一致を無効化**: 正規化後 1-2 文字 CJK 名は Levenshtein 距離 1 で他人を巻き込みやすい（`木` ↔ `林` ↔ `森`）。`lookup_email_by_clinic_and_person` で `len(normalized_target) < 3` のときは完全一致のみ採用し、ファジーは WARNING でスキップ。**(F-05/F-11) サイレント auto-create の解消**: `_ensure_sheet_with_header` を `bool` 戻り値（新規作成したか）に変更し、`_ensure_master_sheet` が新規作成時に WARNING を出す（スプレッドシート ID 込み）。`read_master_records` も 0 件取得時は INFO → WARNING に格上げ。出力一覧シートと医院シートは新規作成が正常運用なので INFO のまま。**(F-09) discover モードのセミナー間誤送信防止**: `RunConfig.from_discovered` で `master_sheet_name = f"{target_folder_name}_参加者マスター"` を派生。全セミナーが既定タブを共有して別セミナーの参加者に誤送信するリスクを除去。**(F-02) 医院番号ゼロパディング桁数違いの吸収**: `_normalize_clinic_number` を追加し `"001"` ↔ `"00001"` を同一医院として扱う。`lookup_clinic_name` と `lookup_email_by_clinic_and_person` の両方で使用。**(F-10) Q2/Q3/Q4 YAML コメント追加**: 既定タブ共有による意図しない参加者リスト共有を運用者に明示。**(F-06) 同一メール複数人下書き**: 共有メール（家族・医院）に異なる個人名が紐づくとき、件名・本文に `"先頭名 ほかN名"` を入れて受信者の混乱を防ぐ。**(pdf_paths 型一貫性)**: メール空経路の `gmail_client.create_draft(pdf_paths=...)` を単一 `Path` から `list[Path]` ラップに統一。テスト 477 → 499 件（22 件追加: 短名ファジーガード 4 / 桁数正規化 3 / 新規作成 WARNING 4 / 0 件 WARNING 2 / discover 派生 2 / グループ化下書き main 4 / batch 3）。P-022 を追加。教訓: (1) **読み取り API が副作用としてリソースを生成すべきではない**。少なくとも生成した瞬間は明示的に WARNING で運用者に伝えること。(2) **CJK 短名のファジー一致は誤マッチ率が高い**。距離ベースの match は文字列長で閾値を変えるか、短い名前では無効化する。(3) **「全セミナー共通の既定値」は誤送信の温床**。セミナー固有のリソース（マスターシート、出力フォルダ、メンバーリスト）は **必ずセミナー名 prefix で物理分離** すること。
- **2026-05-29**: 医院フォルダ名を参加者マスターの確定医院名へ同期（既存フォルダもリネーム、P-019 の限定的緩和）。要望は「PDF（ファイル名先頭）の医院番号 → 参加者マスター照合 → 医院名をフォルダ名に反映」。新規フォルダは P-021 で既にマスター医院名を最優先使用済みだったが、**同じ医院番号の既存フォルダは P-019 の「リネームしない」仕様で再利用時に旧名のまま**残り、マスター登録前に AI 抽出名で作られたフォルダにマスター医院名が反映されなかった（＝真の gap）。対応: `find_or_create_clinic_folder` / `upload_pdf_to_clinic_person` に `clinic_name_authoritative: bool = False` を追加。**マスター由来の確定名のときだけ**（`bool(clinic_name_from_master)`）、既存フォルダ名が確定名と異なれば `files().update` でリネーム。AI 抽出値（`authoritative=False`）ではリネームしない＝`三浦歯科` ↔ `三浦歯科医院` の往復 churn を防ぐ P-019 の意図は維持。リネームはフォルダ ID・URL 不変なので医院フォルダ URL シートの既存リンクは保持。リネーム失敗は WARNING のみで続行（フォルダ ID で処理継続、1000 件中 1 件の失敗で全体を止めない）。呼び出し 3 経路（main / batch_main 本体 / batch_main 添付資料）で確定判定を渡す（添付資料は `master_records` から再判定）。テスト 507 → 511 件（rename 実行 / 一致時無動作 / 非確定時非リネーム / 失敗非致命 の 4 件）。教訓: (1) **要望された機能が「ほぼ既存」のとき、本当の差分は意図的な設計判断の中にある**。今回の真の gap は P-019 の「リネームしない」で、新規命名ロジックではなかった。コードを読み「どこまで出来ていて何が足りないか」を特定してから実装方針を確認した。(2) **音声入力由来の用語ゆれ（委員 vs 医院）と無関係な末尾句（準備中）は実装前に必ず確認する**。コードベースの実語彙（医院 / 参加者）に照らして 3 点確認し、「委員＝医院の言い間違い」「準備中は無視」と判明、誤実装を回避。(3) **意図的な抑制（P-019 のリネーム停止）を緩和するときは元の意図を壊さない条件を足す**。"authoritative なソース由来のときだけ" という条件で churn 防止（元の目的）と反映（新要望）を両立。
- **2026-05-29**: 自動検出モードの参加者マスター参照先を共有「参加者マスター」へ変更（F-09 を撤回）。ユーザー報告「フォルダ名の医院名がスプレッドシートの医院名と一致しない」の根本原因。自動検出モード（`--target-folder テストN`）は F-09（P-022）で `master_sheet_name = f"{target_folder_name}_参加者マスター"`（例 `テスト9_参加者マスター`）を読んでいたが、ユーザーは実データを共有の `参加者マスター` タブに入力していた。システムは空の per-folder タブ（`read_master_records` がヘッダーのみで自動作成）を読み → `lookup_clinic_name` が "" を返し → 医院名が AI 抽出値にフォールバック → フォルダ名がマスター医院名と不一致、かつ Gmail 下書きも宛先空、という事故が起きていた。対応: `discover.RunConfig.from_discovered` の `master_sheet_name` を `MASTER_SHEET_NAME`（共有 `参加者マスター`）に変更。これでプロファイルモード・自動検出モードの両方が同じ共有タブを読む。トレードオフ: F-09 が防いでいた「全セミナー共有タブによるセミナー取り違え」リスクは運用規律に委ねる（実行前に `参加者マスター` の中身を対象セミナーのデータへ差し替える＝ユーザーの既存運用、2026-05-26 エントリと整合）。下書きは自動送信されない（レビュー前提）ため、誤タブのままでも送信事故には直結しない。test_discover の F-09 回帰テスト 2 件を「共有タブを読む」検証に置換（511 件維持）。README の医院フォルダ命名記述も PR #41 のリネーム挙動に合わせて正確化。教訓: (1) **「安全のための分離」と「ユーザーの実運用」が食い違うと、分離はサイレントな空読みを生む**。F-09 は誤送信を防ぐ意図だったが、ユーザーが共有タブ運用だったため逆に「マスター未参照」を量産した。安全機構は実運用フローに沿わせる（沿わせられないなら空タブ実行時に明示警告/HARD FAIL で気づかせる — `read_master_records` の 0 件 WARNING は既にあるが運用者の目に入っていなかった）。(2) **同一論点で過去に相反する判断（2026-05-26 の単一タブ運用 vs F-09 の per-folder 分離）が残っていると事故になる**。最新のユーザー意図を確認して一方に寄せ、矛盾を残さない。
- **2026-05-29**: CI「Python テスト」の transient 失敗を root-cause 修正（フォント DL リトライ）。PR #42 で CI のみ失敗（ローカル / clean worktree / 最新依存 venv の全てで 511 passed のため再現せず）。原因: `test_pdf_creator` / `test_pdf_merger` が実 `ensure_fonts()` を呼び、`assets/*.ttf`（.gitignore で fresh checkout に不在）を GitHub raw から都度ダウンロードしていた。リトライ無しのため一時的ネットワーク障害（5xx / タイムアウト）で即 `RuntimeError` → テスト失敗。ローカルはフォントがキャッシュ済みで早期 return するため再現しない（＝CI 限定 flaky の典型）。修正: `ensure_fonts()` を指数バックオフ（2/4/8 秒）最大 4 回リトライ化（P-017 と同思想、本番 1000 件実行の CDN 一時障害耐性も向上）。未使用の `fonts` dict / `base_url` を削除。pytest 511 → 514 件。教訓: (1) **テストが実ネットワーク I/O に依存すると CI 限定 flaky になる**。ローカルはキャッシュ / 既存ファイルで隠れて気づけない。CI-only 失敗は「fresh checkout で初めて走る I/O（DL・ファイル生成）」をまず疑う。(2) **切り分けは CI 環境に寄せる**（clean worktree + 最新依存 venv）。それでも再現しなければ「外部依存 × 実行タイミング」を疑う。(3) **外部取得は最初からリトライ前提で書く**（quota 用 throttle とは別に、一過性エラー用のバックオフ retry）。
- **2026-06-02**: 本番ラン途中のクレジット切れに fail-fast 停止を導入（reproduce-first、P-024）。本番 GHA ラン 164 件中 49 件が同一エラー `BadRequestError: credit balance is too low` で失敗（88 件成功 → 残高ゼロ → 残り全件失敗）。真因は請求側（残高切れ）で、本 PR はそれを早期検知して停止する**堅牢化**（49 件の実消去にはクレジット追加が必要）。RCA: `main.run` / `batch_main` が per-PDF 例外を一律 `except Exception: continue` で握りつぶし、「以降のどの API 呼び出しも必ず失敗する恒久条件」と「1 件固有の失敗」を区別していなかったため、残高切れ後も 49 回無駄に API を叩きエラーログを乱立させた。reproduce-first で先に失敗テスト（残高切れ 400 / 認証エラーをモックし「ランが即停止し残り PDF を処理しない／無駄に API を叩かない」を検証）を書いて red を確認 → 実装で green。修正: `comment_generator` に `PermanentRunFailureError` + `is_permanent_run_failure`（型 = AuthenticationError/PermissionDeniedError、汎用 400 は message の billing 文言で判定）+ `permanent_failure_message` を追加。`generate_comment_with_metadata` は恒久エラーをリトライせず即 `PermanentRunFailureError` へ変換送出（一過性リトライ tuple と分離）、request-specific 400（プロンプト過大等）は従来通り即 raise（per-PDF fail-soft）。`main.run` は `except PermanentRunFailureError` で break → 添付資料パススルーもスキップ → 成功済みの下書き flush + 一時ファイル削除 → 「中止」マーカー追記 → 例外再送出で GHA 非ゼロ終了。Batch は `submit_batch`/`get_batch_status`/`get_batch_results` の 3 Anthropic 呼び出しで変換し、`step2`/`step3` 経由で `run` 外へ自然伝播（握りつぶす except なしを確認）。横展開 grep: Anthropic 呼び出しは `comment_generator` の 4 箇所のみで全て対処済み、永続エラーを握りつぶして無駄継続する箇所は他に無し。落とし穴: `except comment_generator.PermanentRunFailureError`（モジュール経由参照）は `comment_generator` 全体をモックする既存テストで `except <MagicMock>` → `TypeError` を誘発し integration smoke 3 件を巻き込んだため、例外クラスを `main.py` へ直接 import して解消。pytest 568 → 583 件（+15: classifier 8 / 即送出 3 / main halt 2 / batch halt 2）、mypy Success 維持。reporter: 本番ログ全件解析（ユーザー報告）。

- **2026-06-02**: Batch 回収パス（`--step results --batch-id`）が出力 0 件で終わる no-op を根本修正（P-026）。本番で Batch 投入後、別 GHA ランで `--step results --batch-id <id>` を実行しても PDF/Drive/シートが一切生成されなかった。RCA は 3 層: (1) `run()` の step4 が `step in ("all","pdfs")` ゲートにあり `results` から呼ばれない（結果取得して即終了。README は `--step results` を回収コマンドと記載していたが no-op）。(2) 仮に step4 に入っても items を `batch_prep.json` から読むが、別ジョブで死んだ回収ランには同ファイルが無い（`logs/` は gitignore・artifact も復元されない）→ 空ループで「成功 0 件」の無言失敗。(3) `custom_id` が位置依存（`item_0001`…、シート dedup 後の Drive 走査順で連番付与）で、Drive を再走査しても並び・dedup がずれて Anthropic 結果（`batch_results.json` の key）と突合できない＝「ただ再走査」では不十分。修正: (D1) `custom_id` を Drive file id 由来の安定 ID（`_custom_id_for_file`、Anthropic 制約 `^[A-Za-z0-9_-]{1,64}$` を超える/異文字は SHA-256 fallback）にし step1 で付与 → 再走査で同一 ID を再現でき結果と確実に突合。(D2) `reconstruct_items_from_drive` 新設（`list_pdfs` + ファイル名分類のみ、本文 DL/抽出なしの軽量再構築。添付資料 `batch_attachments.json` も再構築）。(D3) `_resolve_items_for_step4`（`batch_prep.json` 優先＝旧 positional バッチも復元、無ければ Drive 再走査）。(D4) ルーティングは `is_recovery = step=="results" and batch_id is not None`（`batch_id.txt` 補完前に確定）で「明示 batch_id の results のみ step4 完走」。discrete `results`（batch-orchestrator の分割運用）は従来通り結果取得のみ＝長時間ポーリング直後の PDF 生成で GHA 6h 超過を防ぐ。(D5) `results` 非空 & items 空は `RuntimeError` で loud 停止（無言 0 件撲滅）。横展開: `custom_id` は `comment_generator.create_batch_requests` が pass-through なので step1 の付与変更だけで全段に波及。E2E smoke 6 件が旧 positional key をハードコードしていたため file id 由来 key へ更新。pytest 646 → 652 件（+12: custom_id 安定性 2 / Drive 再構築 2 / items 解決 3 / loud guard 1 / 回収 E2E 2 / discrete 非実行 1、＋ smoke 6 件修正）、mypy は既存の yaml/requests stub 警告のみ（無関係）。教訓: (1) **「ステップ分割で再開可能」を謳う設計は、各 step が前段の永続物に依存する箇所を列挙し、永続物が無い経路（＝本当の障害時）を実際に通せるか検証する**。今回 step4 は `batch_prep.json` 前提で、それが無い「真の回収シナリオ」が未検証だった。(2) **再構築のキーは位置でなく内容で**。連番 custom_id は「同じ入力集合・同じ順序」を暗黙の前提にしており再走査・dedup 変動で壊れる。冪等な突合には content-addressed ID を使う。(3) **無言の 0 件成功は最悪の失敗**。回収が空振りしても完了マーカーは「成功 0 件」を書くため、loud fail を入れて運用者が気づけるようにする。reporter: ユーザー（本番 67 件の回収不能報告）。
