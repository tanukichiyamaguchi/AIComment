# Google Form + Apps Script で非開発者にワークフローを起動してもらう

このドキュメントは **リポジトリ所有者向け** のセットアップ手順書です。
GitHub アカウントを持たない人（例: 事務スタッフ）が Google フォームに数字を入力するだけで、`Generate Jissen Comments` ワークフローを起動できる仕組みを構築します。

## 全体構成

```
[実行者]
  ↓ フォーム送信
[Google フォーム]
  ↓ onSubmit トリガー
[Apps Script]
  ↓ POST /repos/{owner}/{repo}/actions/workflows/generate_comments.yml/dispatches
[GitHub API]
  ↓ workflow_dispatch
[Generate Jissen Comments ワークフロー]
```

- 実行者は **GitHub アカウント不要**。Google フォームの URL を踏むだけ
- 認証は Apps Script に保存した **Personal Access Token (PAT)** 1 本で行う
- 想定セットアップ所要時間: **30〜45 分**

---

## A. GitHub Personal Access Token (PAT) の作成

Apps Script が GitHub Actions を起動するための認証トークンを発行します。

1. ブラウザで [https://github.com/settings/tokens?type=beta](https://github.com/settings/tokens?type=beta) を開く
   - もしくは GitHub 右上のアバター → 「Settings」→ 左メニュー「Developer settings」→「Personal access tokens」→「**Fine-grained tokens**」
2. 右上「**Generate new token**」ボタンを押す
3. 入力項目:
   | 項目 | 設定値 |
   |------|--------|
   | Token name | `aicomment-form-trigger`（任意） |
   | Expiration | 任意（推奨: 1 year。期限切れ前に再発行が必要） |
   | Resource owner | `tanukichiyamaguchi` |
   | Repository access | 「**Only select repositories**」を選び、`tanukichiyamaguchi/AIComment` のみ追加 |
4. **Repository permissions** セクションをスクロールし、以下を設定:
   | 権限 | 設定値 |
   |------|--------|
   | **Actions** | **Read and write**（必須・workflow_dispatch のため） |
   | **Metadata** | Read-only（Actions を選ぶと自動で付与される） |
5. ページ下部「**Generate token**」を押す
6. **表示されたトークン値（`github_pat_...` で始まる文字列）を必ずコピーしてメモ帳等に保管**
   - この画面を閉じると **二度と表示されません**
   - 紛失した場合は再発行（古いトークンは削除）

> セキュリティ注意: PAT は GitHub アカウントの権限を肩代わりします。Apps Script のスクリプトプロパティ以外には保存しない、メールや Slack で送らない、Git にコミットしない。

---

## B. Google フォームの作成

1. ブラウザで [https://forms.google.com](https://forms.google.com) を開く
2. 「**空白**」を選んで新規フォーム作成
3. タイトルを **「じっせん君コメント生成 実行フォーム」** に変更
4. 説明文（任意）に下記を貼り付け:
   ```
   このフォームを送信すると、じっせん君コメント生成の処理が GitHub Actions で開始されます。
   通常はそのまま「送信」を押してください。
   ```
5. **質問 1 を追加**:
   - 質問文: **「件数（0なら全件）」**
   - 回答形式: **「記述式」**（短文回答）
   - 右下「必須」を ON
   - 三点メニュー →「回答の検証」→「数値」「以上」「0」、エラーメッセージ「0 以上の整数を入力してください」
   - 三点メニュー →「説明」を表示 → 「初回は 5 件などでテスト推奨」と入力
   - **デフォルト値の付け方**: 質問のメニュー（三点）から「説明」を使い「初回は **0** で全件、または **5** などでテスト」とガイドする（Google フォームには厳密な "デフォルト値" 機能がないため、説明文と回答シートでカバー）
6. 右側ツールバーの「**＋**」で **質問 2 を追加**:
   - 質問文: **「Batch API を使う（50%割引、推奨）」**
   - 回答形式: **「ラジオボタン」**
   - 選択肢:
     - `はい`
     - `いいえ`
   - 右下「必須」を ON
   - 三点メニュー →「説明」→「通常はそのまま『はい』で OK。即時実行が必要なときだけ『いいえ』」と入力
7. 右側ツールバーの「**＋**」で **質問 3 を追加**:
   - 質問文: **「プロファイル（処理対象の四半期 / 文書種別）」**
   - 回答形式: **「プルダウン」**（または「ラジオボタン」でも可）
   - 選択肢（**スペル完全一致が必須** — Apps Script で素通しするため）:
     - `jissen_default`
     - `jissen_2024_q1`
     - `jissen_2024_q2`
     - `jissen_2024_q3`
     - `jissen_2024_q4`
   - 右下「必須」を ON
   - 三点メニュー →「説明」→「通常は `jissen_default`（既存挙動）。期間別の出力を分けたいときだけ該当の四半期を選ぶ」と入力
8. 右側ツールバーの「**＋**」で **質問 4 を追加**（フォルダ自動検出モード用、**任意**）:
   - 質問文: **「対象フォルダ名（自動検出モード）」**
   - 回答形式: **「記述式」**（短文回答）
   - 右下「必須」は **OFF**（空欄なら従来の profile 動作）
   - 三点メニュー →「説明」→「`DRIVE_INPUT_ROOT` 配下のサブフォルダ名を入力すると、profile/Secret 追加なしで処理対象を切り替えできます（例: `2024_Q1_実践事例`）。候補一覧を見たい場合は `__list__` を入力すると GitHub Actions のログに表示されます。**この欄を埋めると質問 3 のプロファイル選択は無視されます**」と入力
   - 補足: フォルダ自動検出モードを使うには、GitHub Secrets に `DRIVE_INPUT_ROOT` / `DRIVE_OUTPUT_ROOT` を別途登録しておく必要があります。README の「フォルダ自動検出モード」セクションを参照
9. 上部の「**回答**」タブを開く → 緑色のスプレッドシートアイコン「**スプレッドシートにリンク**」を押す
   - 「**新しいスプレッドシートを作成**」→ 名前は **「じっせん君コメント生成 実行ログ」** など
   - 既存の出力一覧シートとは **別ファイル** にすること（混在させない）
10. 上部「**プレビュー（目のアイコン）**」で動作確認

---

## C. Apps Script の設定

### C-1. スクリプトエディタを開く

1. フォーム編集画面の右上「**三点メニュー（︙）**」を押す
2. 「**スクリプトエディタ**」を選択 → 新しいタブで Apps Script エディタが開く
3. プロジェクト名を **「AIComment Form Trigger」** に変更（上部のタイトルをクリック）

### C-2. コードを貼り付け

エディタ初期状態の `function myFunction() {}` を **すべて削除** し、後述「**D. Apps Script コード全文**」の内容を貼り付けて保存（Ctrl+S / Cmd+S）。

### C-3. スクリプトプロパティを登録

PAT などの秘密値をコードに直書きしないため、スクリプトプロパティに登録します。

1. 左メニュー「**プロジェクトの設定（歯車アイコン）**」を開く
2. 下部「**スクリプト プロパティ**」→「**スクリプト プロパティを追加**」を 5 回押し、以下を 1 行ずつ登録:

   | プロパティ | 値 |
   |-----------|-----|
   | `GITHUB_PAT` | A で取得した PAT（`github_pat_...`） |
   | `GITHUB_OWNER` | `tanukichiyamaguchi` |
   | `GITHUB_REPO` | `AIComment` |
   | `WORKFLOW_FILE` | `generate_comments.yml` |
   | `WORKFLOW_REF` | `main` |

3. 「**スクリプト プロパティを保存**」ボタンを押す

### C-4. トリガーを設定

1. 左メニュー「**トリガー（時計アイコン）**」を開く
2. 右下の青いボタン「**＋ トリガーを追加**」を押す
3. ダイアログで以下を選択:
   | 項目 | 値 |
   |------|-----|
   | 実行する関数を選択 | `onFormSubmit` |
   | 実行するデプロイを選択 | `Head` |
   | イベントのソースを選択 | **フォームから** |
   | イベントの種類を選択 | **フォーム送信時** |
   | エラー通知設定 | 「今すぐ通知を受け取る」推奨 |
4. 「**保存**」を押す
5. Google アカウント認可ダイアログが出る:
   - 自分の Google アカウントを選択
   - 「このアプリは確認されていません」警告 → 「**詳細**」→「**AIComment Form Trigger (安全ではないページ) に移動**」をクリック
   - 権限一覧（フォーム回答の読み取り・外部 URL アクセス）を確認 → 「**許可**」

> 警告画面が出るのはあなた本人が作ったアプリだからで正常です。GitHub に公開・配布しない限りは「未確認」のままで問題ありません。

---

## D. Apps Script コード全文（コピペ用）

下記コードを Apps Script エディタにそのまま貼り付けてください。外部ライブラリへの依存はありません。

```javascript
/**
 * AIComment Form Trigger
 *
 * Google フォーム送信を受けて、GitHub Actions の
 * `generate_comments.yml` ワークフローを workflow_dispatch で起動する。
 *
 * 必要なスクリプトプロパティ:
 *   - GITHUB_PAT      : Fine-grained PAT (Actions: Read and write)
 *   - GITHUB_OWNER    : 例) tanukichiyamaguchi
 *   - GITHUB_REPO     : 例) AIComment
 *   - WORKFLOW_FILE   : 例) generate_comments.yml
 *   - WORKFLOW_REF    : 例) main
 *
 * フォームの想定質問:
 *   1. 「件数（0なら全件）」 : 短文回答 / 数値
 *   2. 「Batch API を使う（50%割引、推奨）」 : ラジオボタン「はい」「いいえ」
 *   3. 「プロファイル（処理対象の四半期 / 文書種別）」 : プルダウン
 *        選択肢は jissen_default / jissen_2024_q1 〜 q4 のいずれか
 *   4. （任意）「対象フォルダ名（自動検出モード）」 : 短文回答
 *        DRIVE_INPUT_ROOT 配下のサブフォルダ名。入力されれば profile より優先。
 *        "__list__" を入れると GitHub Actions のログに候補一覧が出力される。
 */

// プロファイル名の許可リスト（ホワイトリスト）。
// ここに無い値はフォームから送られても弾く（GitHub Actions に不正入力を流さない）。
var ALLOWED_PROFILES = [
  'jissen_default',
  'jissen_2024_q1',
  'jissen_2024_q2',
  'jissen_2024_q3',
  'jissen_2024_q4',
];

// ============================================================================
// エントリポイント: フォーム送信時に Apps Script が自動実行
// ============================================================================
function onFormSubmit(e) {
  const startedAt = new Date();
  console.log('onFormSubmit triggered at', startedAt.toISOString());

  try {
    const config = loadConfig_();
    const inputs = parseFormResponse_(e);

    console.log('Parsed inputs:', JSON.stringify(inputs));

    const result = dispatchWorkflow_(config, inputs);

    console.log('Dispatch succeeded. status=', result.status);
    writeStatusToSheet_(e, '起動成功（' + formatJst_(startedAt) + '）');
  } catch (err) {
    console.error('onFormSubmit failed:', err && err.stack ? err.stack : err);
    try {
      writeStatusToSheet_(e, '起動失敗: ' + (err && err.message ? err.message : String(err)));
    } catch (writeErr) {
      console.error('Failed to write status to sheet:', writeErr);
    }
    // 例外を再 throw して Apps Script のエラー通知メールを発火させる
    throw err;
  }
}

// ============================================================================
// 設定の読み込み（スクリプトプロパティから）
// ============================================================================
function loadConfig_() {
  const props = PropertiesService.getScriptProperties();
  const required = ['GITHUB_PAT', 'GITHUB_OWNER', 'GITHUB_REPO', 'WORKFLOW_FILE', 'WORKFLOW_REF'];
  const config = {};
  const missing = [];

  required.forEach(function (key) {
    const value = props.getProperty(key);
    if (!value) {
      missing.push(key);
    } else {
      config[key] = value;
    }
  });

  if (missing.length > 0) {
    throw new Error(
      'スクリプトプロパティが未設定です: ' + missing.join(', ') +
      ' （プロジェクトの設定 → スクリプトプロパティ から追加してください）'
    );
  }

  return config;
}

// ============================================================================
// フォーム回答から workflow_dispatch の inputs を構築
// ============================================================================
function parseFormResponse_(e) {
  if (!e || !e.response) {
    throw new Error('フォームイベント (e.response) が取得できません。トリガー種別が「フォーム送信時」になっているか確認してください。');
  }

  const itemResponses = e.response.getItemResponses();

  let testCount = '0';
  let batchMode = 'true';
  let profile = 'jissen_default';
  let targetFolder = '';

  itemResponses.forEach(function (itemResponse) {
    const title = (itemResponse.getItem().getTitle() || '').trim();
    const answer = String(itemResponse.getResponse() || '').trim();

    if (title.indexOf('件数') !== -1) {
      const parsed = parseInt(answer, 10);
      if (isNaN(parsed) || parsed < 0) {
        throw new Error('「件数」には 0 以上の整数を入力してください。入力値: ' + answer);
      }
      testCount = String(parsed);
    } else if (title.indexOf('Batch') !== -1 || title.indexOf('バッチ') !== -1) {
      // 「はい」「いいえ」を boolean 文字列に変換
      // GitHub API は workflow_dispatch の boolean を文字列で受け取る
      if (answer === 'はい' || answer.toLowerCase() === 'yes' || answer === 'Y') {
        batchMode = 'true';
      } else if (answer === 'いいえ' || answer.toLowerCase() === 'no' || answer === 'N') {
        batchMode = 'false';
      } else {
        throw new Error('「Batch API を使う」には「はい」「いいえ」のいずれかを選んでください。入力値: ' + answer);
      }
    } else if (title.indexOf('対象フォルダ') !== -1 || title.indexOf('target_folder') !== -1) {
      // フォルダ自動検出モード。空欄なら profile を使う後方互換動作。
      // 入力されれば中身をそのまま渡す（Drive 側のフォルダ命名は自由なため
      // ホワイトリスト照合不可。Python 側で表記揺れ吸収しつつマッチさせる）。
      targetFolder = answer;
    } else if (title.indexOf('プロファイル') !== -1 || title.toLowerCase().indexOf('profile') !== -1) {
      // ホワイトリスト照合（タイポや未知の値は拒否）
      if (ALLOWED_PROFILES.indexOf(answer) === -1) {
        throw new Error(
          '「プロファイル」には次のいずれかを選んでください: ' +
          ALLOWED_PROFILES.join(', ') + '。入力値: ' + answer
        );
      }
      profile = answer;
    }
  });

  return {
    test_count: testCount,
    batch_mode: batchMode,
    profile: profile,
    target_folder: targetFolder,
  };
}

// ============================================================================
// GitHub Actions API を叩いて workflow_dispatch を起動
// ============================================================================
function dispatchWorkflow_(config, inputs) {
  const url = 'https://api.github.com/repos/' +
              encodeURIComponent(config.GITHUB_OWNER) + '/' +
              encodeURIComponent(config.GITHUB_REPO) + '/actions/workflows/' +
              encodeURIComponent(config.WORKFLOW_FILE) + '/dispatches';

  const payload = {
    ref: config.WORKFLOW_REF,
    inputs: {
      profile: inputs.profile,
      target_folder: inputs.target_folder,
      batch_mode: inputs.batch_mode,
      test_count: inputs.test_count,
    },
  };

  console.log('POST', url, 'payload=', JSON.stringify(payload));

  const options = {
    method: 'post',
    contentType: 'application/json',
    headers: {
      'Authorization': 'Bearer ' + config.GITHUB_PAT,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'AIComment-Form-Trigger',
    },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  };

  const response = UrlFetchApp.fetch(url, options);
  const status = response.getResponseCode();
  const body = response.getContentText();

  if (status === 204) {
    // 成功時は本文なし
    return { status: status };
  }

  if (status === 401) {
    throw new Error('GitHub 認証エラー (401): PAT が無効か期限切れです。スクリプトプロパティ GITHUB_PAT を再設定してください。');
  }
  if (status === 403) {
    throw new Error('GitHub 権限エラー (403): PAT に Actions: Read and write 権限がありません。または対象リポジトリへのアクセスが許可されていません。');
  }
  if (status === 404) {
    throw new Error('GitHub リソースが見つかりません (404): owner / repo / workflow_file / ref のいずれかが間違っています。設定値: ' +
                    config.GITHUB_OWNER + '/' + config.GITHUB_REPO + ' workflow=' + config.WORKFLOW_FILE + ' ref=' + config.WORKFLOW_REF);
  }
  if (status === 422) {
    throw new Error('GitHub バリデーションエラー (422): inputs の型または ref が不正です。本文: ' + body);
  }
  throw new Error('GitHub API 予期せぬレスポンス (' + status + '): ' + body);
}

// ============================================================================
// 回答シートに「ステータス」列を作って結果を書き込む
// ============================================================================
function writeStatusToSheet_(e, statusText) {
  // e.range は回答シート上の追加行レンジ。末尾の隣セルにステータスを書く
  if (!e || !e.range) {
    console.warn('e.range が無いためステータスを書き込めません。');
    return;
  }
  const sheet = e.range.getSheet();
  const row = e.range.getRow();
  const lastCol = sheet.getLastColumn();

  // ヘッダー行 (row=1) を確認し、「ステータス」列が無ければ追加
  const headerRange = sheet.getRange(1, 1, 1, lastCol);
  const headers = headerRange.getValues()[0];
  let statusCol = headers.indexOf('ステータス') + 1; // 1-origin

  if (statusCol === 0) {
    statusCol = lastCol + 1;
    sheet.getRange(1, statusCol).setValue('ステータス');
  }

  sheet.getRange(row, statusCol).setValue(statusText);
}

// ============================================================================
// 手動テスト用: トリガー無しでも実行できる関数
// （Apps Script エディタで関数を `testDispatch` にして「実行」すると、
//   ダミー入力で workflow_dispatch を試せる）
// ============================================================================
function testDispatch() {
  const config = loadConfig_();
  const inputs = {
    test_count: '1',
    batch_mode: 'false',
    profile: 'jissen_default',
    target_folder: '',
  };
  const result = dispatchWorkflow_(config, inputs);
  console.log('testDispatch OK', result);
}

// ============================================================================
// 手動テスト用 2: フォルダ自動検出モードの疎通確認
// ============================================================================
function testDispatchTargetFolder() {
  const config = loadConfig_();
  // "__list__" を指定すると GitHub Actions のログに候補一覧が出力される
  const inputs = {
    test_count: '0',
    batch_mode: 'false',
    profile: '',
    target_folder: '__list__',
  };
  const result = dispatchWorkflow_(config, inputs);
  console.log('testDispatchTargetFolder OK', result);
}

// ============================================================================
// JST 表記ヘルパー
// ============================================================================
function formatJst_(date) {
  return Utilities.formatDate(date, 'Asia/Tokyo', 'yyyy-MM-dd HH:mm:ss');
}
```

---

## E. テスト方法

### E-1. Apps Script 単体で疎通確認

1. Apps Script エディタ上部の関数選択ドロップダウンで `testDispatch` を選択
2. 「**実行**」ボタンを押す
3. 初回は認可ダイアログが出る → 許可
4. 下部「**実行ログ**」に `testDispatch OK { status: 204 }` が出れば成功
5. ブラウザで GitHub の「Actions」タブを開き、新しい `Generate Jissen Comments` のワークフロー実行が **test_count=1 / batch_mode=false** で起動していることを確認

### E-2. フォーム経由で end-to-end テスト

1. フォーム編集画面の「**プレビュー（目のアイコン）**」または共有 URL でフォームを開く
2. 件数に `1`、Batch を `いいえ` を選んで「送信」
3. 回答スプレッドシートを開き、「ステータス」列に「**起動成功（時刻）**」と書かれていることを確認
4. GitHub Actions タブで新しい実行が始まっていることを確認

### E-3. 失敗時の切り分け

| 症状 | 確認手順 |
|------|---------|
| ステータス列が書かれない | Apps Script の「実行数」「実行ログ」を確認。トリガーが発火しているか |
| `スクリプトプロパティが未設定です` | C-3 でプロパティが保存されているか再確認 |
| `GitHub 認証エラー (401)` | PAT を再発行し `GITHUB_PAT` を上書き |
| `GitHub 権限エラー (403)` | PAT の「Repository permissions → Actions」が **Read and write** か |
| `GitHub リソースが見つかりません (404)` | `GITHUB_OWNER` / `GITHUB_REPO` / `WORKFLOW_FILE` / `WORKFLOW_REF` のスペルを再確認 |
| `バリデーションエラー (422)` | フォームの質問タイトル（「件数」「Batch」「プロファイル」「対象フォルダ」を含む）が変わっていないか |
| `「プロファイル」には次のいずれかを選んでください` | 質問 3 の選択肢が `jissen_default` / `jissen_2024_q1` 〜 `q4` のスペル完全一致になっているか |
| 自動検出モードで `target_folder ... が DRIVE_INPUT_ROOT 配下に見つかりません` | 入力した「対象フォルダ名」が Drive 上の `DRIVE_INPUT_ROOT` 配下に存在しない。`testDispatchTargetFolder` で `__list__` を投げて候補一覧を Actions ログで確認する |
| 自動検出モードで `DRIVE_INPUT_ROOT が未設定です` | GitHub Secrets に `DRIVE_INPUT_ROOT` / `DRIVE_OUTPUT_ROOT` を登録していない。README の「フォルダ自動検出モード」セクションを参照 |
| GitHub Actions が起動するが処理がエラー | これは Apps Script の責任範囲外。`Generate Jissen Comments` のログを直接確認 |

ネットワーク一時エラー時のリトライ機構は入れていません。フォームを再送信してください。

---

## F. 共有方法と運用上の注意

### F-1. フォーム URL を非開発者に渡す

1. フォーム編集画面の右上「**送信**」ボタンを押す
2. ダイアログでリンクアイコン（鎖マーク）→「**URL を短縮**」を ON にしてコピー
3. URL を Gmail / LINE / Slack 等で対象者に送る

### F-2. アクセス範囲の設定

1. フォーム編集画面の右上「**設定（歯車）**」→「**全般**」タブ
2. 推奨設定:
   - 「**ログインが必要**」: **OFF**（誰でも回答可能）
   - 「**回答を 1 回に制限する**」: OFF（複数回起動できるように）
3. 「リンクを知っているユーザー全員」が回答可能になります

> 「ログインが必要」を ON にすると、組織の Google アカウント保持者のみに限定できます（Google Workspace ドメイン下のフォームの場合）。

### F-3. セキュリティ上の注意

- **このフォーム URL を知っている人は誰でもワークフローを起動できます**
- 起動 = Claude API / Google Drive を消費する = コストが発生する
- 共有範囲は最小限に保つこと（信頼できる事務スタッフのみ）
- 万一漏洩した場合は:
  1. フォーム編集画面 →「設定」→「**回答を受け付ける**」を OFF（一時停止）
  2. PAT を GitHub 側で revoke（古い PAT を無効化）
  3. 新しい PAT を発行し `GITHUB_PAT` を上書き
- **PAT 自体を共有してはいけません**。フォーム URL だけ共有する

### F-4. 月次運用のチェックリスト

- [ ] PAT の有効期限が近づいたら再発行（90日設定の場合は要注意）
- [ ] 回答スプレッドシートの「ステータス」列を見て、`起動失敗` が増えていないか
- [ ] GitHub Actions の実行履歴とフォーム回答数が一致しているか
</content>
</invoke>