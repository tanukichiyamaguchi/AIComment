"""ユーティリティモジュール。ログ設定・フォント自動ダウンロード・ファイル名整形。"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import unicodedata

from src.config import LOG_LEVEL, LOG_FILE, LOGS_DIR, ASSETS_DIR, FONT_REGULAR, FONT_BOLD


# ログファイル肥大化対策（P-023）。1000 PDF 規模の連続実行で
# logs/jissen_comment.log が GB 級まで膨らみ artifact upload と Codespaces
# disk を圧迫する事故が発生したため、100MB × 5 世代でローテーションする。
_LOG_MAX_BYTES = 100 * 1024 * 1024  # 100MB
_LOG_BACKUP_COUNT = 5


def setup_logging() -> logging.Logger:
    """アプリケーション全体のロガーを設定する。

    既に初期化済みなら再設定せずに既存ロガーを返す（多重ハンドラ防止）。
    ファイル出力は ``RotatingFileHandler`` で 100MB × 5 世代に制限する。
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("jissen_comment")
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    # 二重初期化防止: ハンドラを既に持っている場合はそのまま返す。
    # ``run()`` から多段で setup_logging() が呼ばれてもログが多重化しない。
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # コンソール出力
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # ファイル出力（ローテーション付き）
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        encoding="utf-8",
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def mask_email(email: str) -> str:
    """メールアドレスをマスクする（ログ出力用）。"""
    if "@" not in email:
        return "***"
    local, domain = email.rsplit("@", 1)
    if len(local) <= 2:
        masked_local = "*" * len(local)
    else:
        masked_local = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{masked_local}@{domain}"


# Forbidden chars: path separators, Windows-illegal chars, and non-whitespace
# control characters. Whitespace controls (\t \n \r) are intentionally excluded
# so that the subsequent whitespace-collapse step turns them into single spaces.
_FORBIDDEN_FILENAME_CHARS = re.compile(
    r'[\\/:*?"<>|\x00-\x08\x0b\x0c\x0e-\x1f]'
)


def sanitize_filename(
    name: str,
    fallback: str = "untitled",
    max_length: int = 100,
) -> str:
    """ファイル名として安全な文字列に整形する。

    - パス区切り（``/`` ``\\``）と Windows / Drive で問題を起こす特殊文字を除去
    - タブ・改行などの空白制御文字は空白に変換し、連続する空白を1つにまとめる
    - 先頭・末尾の空白とドットを除去
    - 空になったら ``fallback`` を返す
    - ``max_length`` で切り詰め

    Args:
        name: 元の文字列
        fallback: 整形後に空になった場合の代替文字列
        max_length: 上限文字数

    Returns:
        ファイル名として使える文字列
    """
    if not isinstance(name, str):
        name = str(name)
    cleaned = _FORBIDDEN_FILENAME_CHARS.sub("", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        return fallback
    return cleaned[:max_length]


# 実践事例 PDF のファイル名先頭に埋め込まれた管理番号のパターン。
# ``<3〜5桁>-<2桁>-<1桁>``（先頭セグメント = 医院番号は 3〜5 桁可変、
# 2・3 番目のセグメントは桁数固定）。先頭セグメントの ``(...)`` は
# ``extract_clinic_number`` が医院番号として再利用する。
_MANAGEMENT_NUMBER_PATTERN = re.compile(r"^(\d{3,5})-\d{2}-\d")


def extract_management_number(filename: str) -> str:
    """PDFファイル名の先頭から管理番号を抽出する。

    実践事例 PDF はファイル名の先頭に ``NNN-NN-N`` 形式
    （先頭セグメント 3〜5 桁 - 数字2 - 数字1）の管理番号が埋め込まれている。
    例: ``001-01-0実践事例.pdf`` → ``001-01-0``、``00123-45-6.pdf`` → ``00123-45-6``。

    Args:
        filename: PDF のファイル名

    Returns:
        抽出した管理番号（先頭セグメントの桁数に応じて 7〜9 文字）。
        先頭がパターンに合致しない場合は空文字列。
        （呼び出し側で空文字列を検知して warning ログを出すこと）
    """
    if not isinstance(filename, str):
        filename = str(filename)
    match = _MANAGEMENT_NUMBER_PATTERN.match(filename)
    return match.group(0) if match else ""


def extract_clinic_number(filename: str) -> str:
    """PDFファイル名先頭の管理番号から医院番号（最初のハイフンより前）を抽出する。

    管理番号 ``NNN-NN-N`` の先頭セグメント（3〜5桁）が医院番号。
    例: ``001-01-0実践事例.pdf`` → ``001``、``00123-45-6.pdf`` → ``00123``。
    管理番号がファイル名先頭から抽出できない場合は空文字列を返す。

    Args:
        filename: PDF のファイル名

    Returns:
        抽出した医院番号（3〜5桁の数字文字列）。
        先頭がパターンに合致しない場合は空文字列。
    """
    if not isinstance(filename, str):
        filename = str(filename)
    match = _MANAGEMENT_NUMBER_PATTERN.match(filename)
    return match.group(1) if match else ""


# 添付資料 PDF をファイル名から識別するためのマーカー（全角の隅付き括弧込み）。
# このマーカーを含む PDF は実践事例の補足資料であり、AI 処理（テキスト抽出 /
# Claude API 呼び出し / コメントページ生成 / PDF 結合）を一切せず、
# メイン実践事例 PDF と同じ出力フォルダへ元ファイルのままコピーする。
_ATTACHMENT_MARKER = "【添付資料】"


def is_attachment_filename(filename: str) -> bool:
    """ファイル名が添付資料を示すか判定する。

    ファイル名に「【添付資料】」（全角の【】込み）を含む PDF は、
    実践事例の補足資料。AI 処理せず、メインと同じ出力フォルダに
    元ファイルのままコピーする対象。

    Args:
        filename: PDF のファイル名

    Returns:
        ファイル名に添付資料マーカーを含むなら True。
    """
    if not isinstance(filename, str):
        filename = str(filename)
    return _ATTACHMENT_MARKER in filename


def normalize_name_for_match(name: str) -> str:
    """医院名・氏名のマッチング用に正規化する。

    AIが抽出する医院名・氏名は、同じ医院/人物でも軽微な表記揺れ
    （半角/全角、空白の有無）が発生する。Driveフォルダの重複作成を防ぐため、
    ルックアップ時のみこの正規化済み形で比較する。

    変換内容:
        - NFKC 正規化（全角英数字・記号を半角に統一）
        - 全種類の空白文字（半角・全角・タブ等）をすべて除去

    変換しないこと（保守的判定のため）:
        - 大文字小文字（"WKWK" と "wkwk" は別物として扱う）
        - 句読点・記号（"森本歯科" と "森本歯科クリニック" は別物）

    マッチング比較**専用**で、表示・保存には使わない（元の表記を保持する）。
    """
    if not isinstance(name, str):
        name = str(name)
    nfkc = unicodedata.normalize("NFKC", name)
    return re.sub(r"\s+", "", nfkc)


# フォントダウンロードのリトライ設定（CI / 本番の一時的ネットワーク障害対策）。
# assets/*.ttf は .gitignore のため fresh checkout（CI）では毎回ダウンロードが
# 走る。GitHub raw の一時的な 5xx / タイムアウトでテストや本番が落ちないよう、
# 指数バックオフでリトライする（GOOGLE_API_NUM_RETRIES と同じ思想、P-017）。
_FONT_DOWNLOAD_MAX_ATTEMPTS = 4
_FONT_DOWNLOAD_BACKOFF_BASE = 2.0


def ensure_fonts() -> None:
    """NotoSansJPフォントが存在しない場合、GitHub から自動ダウンロードする。

    ``assets/*.ttf`` は .gitignore のため fresh checkout では存在せず、PDF 生成
    時に都度ダウンロードする。一時的なネットワーク障害（GitHub raw の 5xx /
    タイムアウト）で落ちないよう、指数バックオフで
    ``_FONT_DOWNLOAD_MAX_ATTEMPTS`` 回までリトライする。全試行失敗で RuntimeError。
    """
    import time

    import requests

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    missing = [f for f in [FONT_REGULAR, FONT_BOLD] if not f.exists()]
    if not missing:
        return

    logger = logging.getLogger("jissen_comment")

    # NotoSansJP-Regular.ttf はリポジトリにコミット済み。Regular/Bold は同一の
    # variable font なので、ローカルに片方があれば不足分はその実体を複製して
    # 作る（ネットワーク不要）。これで CI / 本番とも実行時ダウンロードに依存せず
    # 安定する。両方とも無い場合のみ、従来どおり GitHub からダウンロードする。
    source = next((f for f in (FONT_REGULAR, FONT_BOLD) if f.exists()), None)
    if source is not None:
        data = source.read_bytes()
        for font_path in missing:
            font_path.write_bytes(data)
            logger.info(f"フォントをローカル複製で生成: {font_path.name}（DL不要）")
        return

    logger.info("NotoSansJPフォントをダウンロード中...")

    # GitHub の google/fonts リポジトリから Variable font を直接取得し、
    # Regular/Bold 両方に同じファイルを使う（1 ファイルで全ウェイトを含む）。
    variable_font_url = (
        "https://github.com/google/fonts/raw/main/ofl/notosansjp/"
        "NotoSansJP%5Bwght%5D.ttf"
    )

    last_err: Exception | None = None
    for attempt in range(1, _FONT_DOWNLOAD_MAX_ATTEMPTS + 1):
        try:
            response = requests.get(variable_font_url, timeout=60)
            response.raise_for_status()
            font_data = response.content
            for font_path in [FONT_REGULAR, FONT_BOLD]:
                if not font_path.exists():
                    font_path.write_bytes(font_data)
                    logger.info(f"フォント保存: {font_path.name}")
            logger.info("フォントのダウンロード完了")
            return
        except Exception as e:  # 一時障害含め全てリトライ対象（最後に送出）
            last_err = e
            if attempt < _FONT_DOWNLOAD_MAX_ATTEMPTS:
                sleep_for = _FONT_DOWNLOAD_BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    f"フォントのダウンロード失敗 "
                    f"(試行 {attempt}/{_FONT_DOWNLOAD_MAX_ATTEMPTS}, "
                    f"{sleep_for:.0f}秒後に再試行): {e}"
                )
                time.sleep(sleep_for)

    logger.error(f"フォントのダウンロードに失敗: {last_err}")
    raise RuntimeError(
        f"NotoSansJPフォントのダウンロードに失敗しました"
        f"（{_FONT_DOWNLOAD_MAX_ATTEMPTS}回試行）。"
        f"手動で {ASSETS_DIR} にフォントファイルを配置してください。"
    ) from last_err
