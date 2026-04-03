from __future__ import annotations

import argparse
import datetime
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# =========================
# 設定
# =========================


def _default_target() -> Path:
    """
    日本語環境では ~/デスクトップ の場合もあるため両対応する。
    """

    home = Path.home()
    desktop_en = home / "Desktop"
    desktop_ja = home / "デスクトップ"
    if desktop_en.is_dir():
        return desktop_en
    if desktop_ja.is_dir():
        return desktop_ja
    return desktop_en


def _default_downloads() -> Path:
    """
    日本語環境では ~/ダウンロード の場合もあるため両対応する。
    """

    home = Path.home()
    dl_en = home / "Downloads"
    dl_ja = home / "ダウンロード"
    if dl_en.is_dir():
        return dl_en
    if dl_ja.is_dir():
        return dl_ja
    return dl_en


TARGET_DIR    = _default_target()
DOWNLOADS_DIR = _default_downloads()
OLLAMA_MODEL  = "llama3"
DRY_RUN       = False

# このスクリプト自身を除外するためのパス
SCRIPT_PATH = Path(__file__).resolve()

# --all-dirs 時のデフォルト整理対象。
# organize() が存在チェックを内包するため、存在しないフォルダが含まれても安全。
DEFAULT_TARGET_DIRS: list[Path] = [TARGET_DIR, DOWNLOADS_DIR]

# すべてのカテゴリフォルダの移動先ルート。
# Desktop / Downloads はファイルの「監視元」であり、フォルダは一切作らない。
BASE_DIR: Path = Path.home() / "Documents" / "DesktopOrganizer"


# ---------- インストーラー ----------

INSTALLER_EXTENSIONS:  frozenset[str] = frozenset({".dmg", ".pkg"})
INSTALLER_MAX_AGE_DAYS: int           = 7

# ---------- スクリーンショット ----------

SCREENSHOTS_MAX_AGE_DAYS: int = 3


# ---------- 拡張子ルール ----------

# インストーラー (.dmg/.pkg) は classify_installer() が担当。
# PDF (.pdf)            は classify_pdf_file()   が担当。
# それぞれ choose_category() の優先順序で保証する。
STATIC_RULES: dict[str, str] = {
    ".zip": "Archives",
    ".rar": "Archives",
    ".7z":  "Archives",
    ".tar": "Archives",
    ".gz":  "Archives",

    ".mp4": "Videos",
    ".mov": "Videos",
    ".mkv": "Videos",

    ".mp3": "Audio",
    ".wav": "Audio",
    ".m4a": "Audio",

    ".xlsx": "Data",
    ".xls":  "Data",
}


# ---------- AI 判定対象 ----------

TEXT_EXTENSIONS: frozenset[str] = frozenset({
    ".txt", ".md",   ".csv",  ".json",
    ".py",  ".js",   ".ts",   ".html",
    ".css", ".yaml", ".yml",  ".xml",  ".log",
})

IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic",
})

ALLOWED_CATEGORIES: frozenset[str] = frozenset({
    "Notes", "Code", "Data", "Document",
    "Images", "Screenshots",
    "Installers", "Archives", "PDF",
    "Videos", "Audio",
    "Other", "Unreadable",
})


# =========================
# データ構造
# =========================


@dataclass
class Stats:
    moved:   int = 0
    skipped: int = 0
    errors:  int = 0
    deleted: int = 0


# =========================
# 二重処理防止キャッシュ
# =========================

# key  : str(path.resolve())
# value: (st_mtime, time.monotonic() at processing)
_PROCESSED_CACHE: dict[str, tuple[float, float]] = {}

_CACHE_WINDOW_S:  float = 30.0   # 同一ファイルを再処理しない秒数
CACHE_TTL_SECONDS: int  = 300    # キャッシュエントリの有効期間（秒）


def cleanup_cache(verbose: bool = False) -> int:
    """
    有効期間（CACHE_TTL_SECONDS）を超えたキャッシュエントリを削除する。
    削除件数を返す。verbose=True のときは DEBUG ログを出す。

    呼び出しタイミング:
    - organize() の先頭で 1 回
    - watch モードでは 50 イベントごとに 1 回
    """

    now         = time.monotonic()
    stale_keys  = [
        key for key, (_, processed_at) in _PROCESSED_CACHE.items()
        if (now - processed_at) > CACHE_TTL_SECONDS
    ]
    for key in stale_keys:
        del _PROCESSED_CACHE[key]

    count = len(stale_keys)
    if verbose and count > 0:
        print(f"DEBUG Cache cleaned: removed {count} entries")
    return count


def should_skip_recently_processed(
    path:     Path,
    window_s: float = _CACHE_WINDOW_S,
) -> bool:
    """
    同一ファイル（同一パス + 同一 mtime）を window_s 秒以内に処理済みなら True。
    mtime が変わったファイル（更新された）は処理を許可する。
    """

    key = str(path.resolve())
    entry = _PROCESSED_CACHE.get(key)
    if entry is None:
        return False

    cached_mtime, processed_at = entry
    try:
        current_mtime = path.stat().st_mtime
    except OSError:
        return False

    same_content  = abs(current_mtime - cached_mtime) < 1.0
    within_window = (time.monotonic() - processed_at) < window_s
    return same_content and within_window


def mark_as_processed(path: Path) -> None:
    """
    ファイルを処理済みとしてキャッシュに登録する。
    stat() 失敗時は mtime=0 で登録し、処理を継続する。
    """

    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    _PROCESSED_CACHE[str(path.resolve())] = (mtime, time.monotonic())


# =========================
# 共通ユーティリティ
# =========================


def is_file_stable(path: Path, wait_s: float = 0.3) -> bool:
    """
    wait_s 秒間ファイルサイズが変化しなければ True（書き込み完了済み）。
    サイズが変わっていたら False（ダウンロード中と判断しスキップ）。
    stat() 失敗時は安全側で False を返す。
    """

    try:
        size_before = path.stat().st_size
        time.sleep(wait_s)
        size_after  = path.stat().st_size
        return size_before == size_after
    except OSError:
        return False


def parse_category(output: str, allowed: set[str], default: str) -> str:
    """
    モデル出力が余計な文字を含む場合があるため、許可されたカテゴリへ正規化する。
    """

    if not output:
        return default

    text = output.strip()

    # 1行目・先頭トークンを優先
    first_line = text.splitlines()[0] if text.splitlines() else text
    token = first_line.strip().split()[0] if first_line.strip().split() else ""
    token = token.strip(".,:;\"'`[](){}")
    if token in allowed:
        return token

    # 可能性がある行（先頭の記号・番号等を除去して）を総当たり
    for line in text.splitlines():
        cleaned = re.sub(r"^[\-\*\d\.\)\s]+", "", line).strip()
        cleaned = cleaned.strip(".,:;\"'`[](){}")
        if cleaned in allowed:
            return cleaned

    return default


def ask_ollama(prompt: str, model: str = OLLAMA_MODEL, timeout_s: int = 120) -> str:
    """
    Ollama にプロンプトを送り、回答文字列を返す。
    """

    if shutil.which("ollama") is None:
        raise RuntimeError("ollama command not found in PATH")

    try:
        result = subprocess.run(
            ["ollama", "run", model, prompt],
            capture_output=True, text=True, timeout=timeout_s,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass

    result = subprocess.run(
        ["ollama", "run", model],
        input=prompt,
        capture_output=True, text=True, timeout=timeout_s,
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Ollama execution failed")

    return result.stdout.strip()


def safe_read_text(file_path: Path, max_chars: int = 1200, max_bytes: int = 2_000_000) -> str:
    """
    ファイルをできるだけ安全に読み込む。読めなければ空文字。
    """

    try:
        if file_path.stat().st_size > max_bytes:
            return ""
        return file_path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    except Exception:
        return ""


def get_safe_destination(folder: Path, filename: str) -> Path:
    """
    同名ファイルがある場合は "stem (N).ext" 形式（macOS Finder 準拠）で衝突を回避する。

    例: file.dmg が存在する場合 → file (1).dmg
    """

    destination = folder / filename
    if not destination.exists():
        return destination

    stem    = destination.stem
    suffix  = destination.suffix
    counter = 1

    while True:
        candidate = folder / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def is_hidden(path: Path) -> bool:
    """
    ドットファイル（.DS_Store / .localized 等 Apple メタファイルを含む）を検出する。
    """

    return path.name.startswith(".")


def get_skip_reason(item: Path) -> Optional[str]:
    """
    スキップすべき理由を文字列で返す。処理してよい場合は None。

    理由一覧:
      "directory"  : フォルダ
      "hidden"     : ドットファイル（.DS_Store 等）
      "self"       : このスクリプト自身
    """

    if item.is_dir():
        return "directory"
    if is_hidden(item):
        return "hidden"
    try:
        if item.resolve() == SCRIPT_PATH:
            return "self"
    except OSError:
        pass
    return None


def should_skip(item: Path) -> bool:
    """get_skip_reason() のラッパー。後方互換性のために残す。"""

    return get_skip_reason(item) is not None


def move_file(
    file_path:     Path,
    category:      str,
    dest_dir:      Path,
    dry_run:       bool = DRY_RUN,
    dest_filename: Optional[str] = None,
) -> Path:
    """
    ファイルを dest_dir 配下のカテゴリフォルダへ移動する。

    dest_dir には常に BASE_DIR を渡すこと。
    dest_filename を指定した場合、その名前で保存する（インストーラーリネーム用）。
    同名衝突は get_safe_destination() で回避する。
    mkdir は exist_ok=True + parents=True で race condition に対応する。
    dry_run=True の場合は移動せず、移動先パスだけ返す。
    """

    target_folder = dest_dir / category
    target_folder.mkdir(exist_ok=True, parents=True)

    filename    = dest_filename if dest_filename else file_path.name
    destination = get_safe_destination(target_folder, filename)

    if not dry_run:
        shutil.move(str(file_path), str(destination))

    return destination


# =========================
# ログユーティリティ
# =========================


def log_move(
    filename:    str,
    destination: Path,
    dry_run:     bool,
    verbose:     bool,
) -> None:
    """
    移動ログを統一フォーマットで出力する。

      verbose=False, dry_run=False : Moved: filename -> destination
      verbose=True,  dry_run=False : INFO Moved: filename -> destination
      verbose=False, dry_run=True  : [DRY RUN] filename -> destination
      verbose=True,  dry_run=True  : INFO [DRY RUN] filename -> destination
    """

    if dry_run and verbose:
        print(f"INFO [DRY RUN] {filename} -> {destination}")
    elif dry_run:
        print(f"[DRY RUN] {filename} -> {destination}")
    elif verbose:
        print(f"INFO Moved: {filename} -> {destination}")
    else:
        print(f"Moved: {filename} -> {destination}")


def log_skip(reason: str, filename: str, verbose: bool) -> None:
    """
    スキップ理由を verbose 時のみ出力する。

    例:
      SKIP (hidden): .DS_Store
      SKIP (recently processed): app.dmg
      SKIP (unstable file): downloading.dmg
    """

    if verbose:
        print(f"SKIP ({reason}): {filename}")


# =========================
# インストーラー判定
# =========================


def is_installer(filename: str) -> bool:
    """
    ファイル名の拡張子が INSTALLER_EXTENSIONS に含まれていれば True を返す。
    大文字・小文字を問わない。

    >>> is_installer("App.dmg")
    True
    >>> is_installer("App.PKG")
    True
    >>> is_installer("readme.txt")
    False
    """

    return Path(filename).suffix.lower() in INSTALLER_EXTENSIONS


def classify_installer(file_path: Path) -> str:
    """
    インストーラーファイルを "Installers" カテゴリに分類して返す。
    is_installer() が True の場合のみ呼び出すこと。
    """

    return "Installers"


# =========================
# インストーラー リネーム
# =========================


def sanitize_filename(name: str) -> str:
    """
    ファイル名（拡張子なし）を安全な文字列へ変換する。

    変換ルール:
      1. 小文字化
      2. 空白を - に変換
      3. 英数字 / ハイフン / アンダースコア / ピリオド以外を除去
      4. 連続するハイフンを 1 つに圧縮
      5. 先頭・末尾のハイフンをトリム

    >>> sanitize_filename("ChatGPT Installer")
    'chatgpt-installer'
    >>> sanitize_filename("Cursor (2.0) Setup!")
    'cursor-2.0-setup'
    """

    name = name.lower()
    name = name.replace(" ", "-")
    name = re.sub(r"[^a-z0-9\-_.]", "", name)
    name = re.sub(r"-{2,}", "-", name)
    name = name.strip("-")
    return name


def build_installer_filename(
    original_name: str,
    now: Optional[datetime.datetime] = None,
) -> str:
    """
    インストーラーの新しいファイル名を生成する。

    形式: installer_YYYY-MM-DD_HHMM_<sanitized-stem><ext>

    例:
      "ChatGPT Installer.dmg"  -> "installer_2026-04-02_2125_chatgpt-installer.dmg"
      "Cursor Installer.pkg"   -> "installer_2026-04-02_2125_cursor-installer.pkg"

    Args:
        original_name: 元のファイル名（拡張子付き）。
        now: タイムスタンプ（省略時は現在日時）。テスト時に固定値を渡せる。
    """

    p    = Path(original_name)
    stem = sanitize_filename(p.stem)
    ext  = p.suffix.lower()
    ts   = (now or datetime.datetime.now()).strftime("%Y-%m-%d_%H%M")
    return f"installer_{ts}_{stem}{ext}"


# =========================
# 古いインストーラー削除
# =========================


def is_old_file(file_path: Path, max_age_days: int = INSTALLER_MAX_AGE_DAYS) -> bool:
    """
    ファイルの最終更新日時が max_age_days 日より古ければ True を返す。
    stat() が失敗した場合は安全側（False）を返す。
    """

    try:
        mtime = datetime.datetime.fromtimestamp(file_path.stat().st_mtime)
        age   = datetime.datetime.now() - mtime
        return age.days > max_age_days
    except OSError:
        return False


def should_delete_installer(
    file_path:    Path,
    max_age_days: int = INSTALLER_MAX_AGE_DAYS,
) -> bool:
    """
    以下をすべて満たすファイルを削除対象とする。

    - 通常ファイルである
    - .dmg または .pkg である
    - max_age_days 日より古い
    """

    return (
        file_path.is_file()
        and is_installer(file_path.name)
        and is_old_file(file_path, max_age_days)
    )


def cleanup_old_installers(
    target_dir:   Path,
    dry_run:      bool,
    verbose:      bool,
    max_age_days: int = INSTALLER_MAX_AGE_DAYS,
) -> int:
    """
    target_dir/Installers 内の古いインストーラーを削除する。

    安全策:
    - 削除対象は Installers フォルダ直下の .dmg / .pkg のみ
    - path.resolve() で Installers フォルダ外への逸脱を検証する
    - dry_run=True の場合はログのみ出力し、実際には削除しない
    - 個別エラーが出ても処理を継続する
    - 削除（または削除予定）件数を返す
    """

    installers_dir = target_dir / "Installers"
    if not installers_dir.is_dir():
        return 0

    try:
        resolved_installers_dir = installers_dir.resolve()
    except OSError:
        return 0

    count = 0
    for file_path in sorted(installers_dir.iterdir()):
        try:
            # Installers フォルダ直下であることを resolve() で確認（パストラバーサル防止）
            if file_path.resolve().parent != resolved_installers_dir:
                continue

            if not should_delete_installer(file_path, max_age_days):
                continue

            if dry_run:
                print(f"INFO [DRY RUN] Would delete old installer: {file_path}")
            else:
                file_path.unlink()
                print(f"INFO Deleted old installer: {file_path}")

            count += 1

        except Exception as e:
            print(f"Error: cleanup {file_path.name} -> {e}")

    return count


# =========================
# 古いスクリーンショット削除
# =========================


def cleanup_old_screenshots(
    base_dir:     Path,
    dry_run:      bool,
    verbose:      bool,
    max_age_days: int = SCREENSHOTS_MAX_AGE_DAYS,
) -> int:
    """
    base_dir/Screenshots 内の古いファイルを削除する。

    安全策:
    - 削除対象は Screenshots フォルダ直下のファイルのみ
    - path.resolve() で Screenshots フォルダ外への逸脱を検証する
    - dry_run=True の場合はログのみ出力し、実際には削除しない
    - 個別エラーが出ても処理を継続する
    - 削除（または削除予定）件数を返す
    """

    screenshots_dir = base_dir / "Screenshots"
    if not screenshots_dir.is_dir():
        return 0

    try:
        resolved_screenshots_dir = screenshots_dir.resolve()
    except OSError:
        return 0

    count = 0
    for file_path in sorted(screenshots_dir.iterdir()):
        try:
            # Screenshots フォルダ直下であることを resolve() で確認（パストラバーサル防止）
            if file_path.resolve().parent != resolved_screenshots_dir:
                continue

            if not file_path.is_file():
                continue

            if not is_old_file(file_path, max_age_days):
                continue

            if dry_run:
                print(f"INFO [DRY RUN] Would delete old screenshot: {file_path}")
            else:
                file_path.unlink()
                print(f"INFO Deleted old screenshot: {file_path}")

            count += 1

        except Exception as e:
            print(f"Error: cleanup screenshot {file_path.name} -> {e}")

    return count


# =========================
# AI 分類
# =========================


def classify_pdf_file(file_path: Path, model: str = OLLAMA_MODEL) -> str:
    """
    PDF ファイルを分類する。

    pdftotext（Homebrew poppler）が利用可能な場合は先頭 2 ページのテキストを
    抽出して AI に判定させる。利用不可・抽出失敗時は "PDF" にフォールバック。

    外部依存なし（pdftotext はオプション扱い）。

    将来 PyMuPDF / pypdf 等を導入する場合は、この関数内の抽出ロジックを
    置き換えるだけでよい。
    """

    if shutil.which("pdftotext") is None:
        return "PDF"

    try:
        result = subprocess.run(
            ["pdftotext", "-l", "2", str(file_path), "-"],
            capture_output=True, text=True, timeout=15,
        )
        text = result.stdout.strip()[:1200]
    except Exception:
        return "PDF"

    if not text:
        return "PDF"

    allowed = {"Document", "Data", "Other", "PDF"}

    prompt = f"""
あなたはファイル整理AIです。
次のPDFの内容を見て、このファイルを以下のどれか1語だけで分類してください。

候補:
- Document
- Data
- Other
- PDF

必ず候補の単語だけを出力してください。説明文は禁止です。

ファイル名:
{file_path.name}

内容（先頭抜粋）:
{text}
"""

    try:
        raw = ask_ollama(prompt, model=model)
        return parse_category(raw, allowed=allowed, default="PDF")
    except Exception:
        return "PDF"


def classify_text_file(file_path: Path, model: str = OLLAMA_MODEL) -> str:
    content = safe_read_text(file_path)

    if not content.strip():
        return "Unreadable"

    allowed = {"Notes", "Code", "Data", "Document", "Other", "Unreadable"}

    prompt = f"""
あなたはファイル整理AIです。
次のテキスト内容を見て、このファイルを以下のどれか1語だけで分類してください。

候補:
- Notes
- Code
- Data
- Document
- Other
- Unreadable

必ず候補の単語だけを出力してください。説明文は禁止です。

ファイル名:
{file_path.name}

内容:
{content}
"""

    try:
        raw = ask_ollama(prompt, model=model)
        return parse_category(raw, allowed=allowed, default="Other")
    except Exception:
        return "Other"


def classify_image_file(file_path: Path, model: str = OLLAMA_MODEL) -> str:
    """
    画像は現段階ではファイル名ベースで分類。
    """

    allowed = {"Screenshots", "Images", "Other", "Unreadable"}

    prompt = f"""
あなたはファイル整理AIです。
次の画像ファイル名を見て、このファイルを以下のどれか1語だけで分類してください。

候補:
- Screenshots
- Images
- Other
- Unreadable

必ず候補の単語だけを出力してください。説明文は禁止です。

ファイル名:
{file_path.name}
"""

    try:
        raw = ask_ollama(prompt, model=model)
        return parse_category(raw, allowed=allowed, default="Images")
    except Exception:
        return "Images"


def classify_unknown_file(file_path: Path, model: str = OLLAMA_MODEL) -> str:
    """
    拡張子不明ファイルは、ファイル名だけで軽くAI判定。
    """

    allowed = {"Document", "Data", "Code", "Other"}

    prompt = f"""
あなたはファイル整理AIです。
次のファイル名から、このファイルを以下のどれか1語だけで分類してください。

候補:
- Document
- Data
- Code
- Other

必ず候補の単語だけを出力してください。説明文は禁止です。

ファイル名:
{file_path.name}
"""

    try:
        raw = ask_ollama(prompt, model=model)
        return parse_category(raw, allowed=allowed, default="Other")
    except Exception:
        return "Other"


# =========================
# 分類本体
# =========================


def choose_category(file_path: Path, model: str = OLLAMA_MODEL) -> Optional[str]:
    """
    ファイルの分類先カテゴリを返す。

    優先順序:
      1. インストーラー（.dmg / .pkg）  → classify_installer()    即決
      2. PDF（.pdf）                    → classify_pdf_file()      内容ベース or フォールバック
      3. 拡張子ルール（STATIC_RULES）   → 即決
      4. テキスト/コード                → classify_text_file()     AI（内容ベース）
      5. 画像                           → classify_image_file()    AI（ファイル名ベース）
      6. その他                         → classify_unknown_file()  AI（ファイル名ベース）
    """

    # 1. インストーラーは専用関数で即決
    if is_installer(file_path.name):
        return classify_installer(file_path)

    ext = file_path.suffix.lower()

    # 2. PDF は内容ベース分類を試みる（フォールバック: "PDF"）
    if ext == ".pdf":
        return classify_pdf_file(file_path, model=model)

    # 3. ルールで即決
    if ext in STATIC_RULES:
        return STATIC_RULES[ext]

    # 4. テキスト/コードは中身でAI判定
    if ext in TEXT_EXTENSIONS:
        return classify_text_file(file_path, model=model)

    # 5. 画像はファイル名でAI判定
    if ext in IMAGE_EXTENSIONS:
        return classify_image_file(file_path, model=model)

    # 6. それ以外はファイル名でAI判定
    return classify_unknown_file(file_path, model=model)


# =========================
# メイン処理
# =========================


def organize(
    target_dir:  Path = TARGET_DIR,
    dry_run:     bool = DRY_RUN,
    model:       str  = OLLAMA_MODEL,
    verbose:     bool = False,
    run_cleanup: bool = True,
) -> Stats:
    """
    target_dir 直下のファイルを一括整理する（単一フォルダ・1回実行モード）。

    処理順序:
      1. 期限切れキャッシュを削除（cleanup_cache）
      2. Installers フォルダ内の古いファイルを削除（run_cleanup=True の場合のみ）
      3. target_dir 直下のスナップショットを取得
      4. 各ファイルを分類 → （インストーラーは）リネーム → 移動

    Args:
        run_cleanup: False にすると cleanup_old_installers をスキップする。
                     organize_all() から呼ばれるときは False を渡し、
                     cleanup は organize_all() 側で一元管理する。
    """

    stats = Stats()

    if not target_dir.exists():
        print(f"対象フォルダが存在しません: {target_dir}")
        return stats

    # 1. 期限切れキャッシュを削除（organize 実行ごとに 1 回）
    cleanup_cache(verbose=verbose)

    # 2. 古いインストーラー/スクリーンショットを先に掃除する（organize_all() 経由のときはスキップ）
    if run_cleanup:
        stats.deleted += cleanup_old_installers(
            BASE_DIR, dry_run=dry_run, verbose=verbose
        )
        stats.deleted += cleanup_old_screenshots(
            BASE_DIR, dry_run=dry_run, verbose=verbose
        )

    # 3. 移動でディレクトリ構成が変わるため、先にスナップショットを取る
    items = list(target_dir.iterdir())

    for item in items:
        try:
            # スキップ判定（理由付き）
            skip_reason = get_skip_reason(item)
            if skip_reason:
                log_skip(skip_reason, item.name, verbose)
                stats.skipped += 1
                continue

            # 二重処理チェック
            if should_skip_recently_processed(item):
                log_skip("recently processed", item.name, verbose)
                stats.skipped += 1
                continue

            category = choose_category(item, model=model)
            if not category:
                stats.skipped += 1
                continue

            if category not in ALLOWED_CATEGORIES:
                category = "Other"

            # インストーラーは移動先のファイル名を安全な形式へ変換する
            dest_filename = (
                build_installer_filename(item.name)
                if category == "Installers"
                else None
            )

            destination = move_file(
                item, category,
                dest_dir=BASE_DIR,
                dry_run=dry_run,
                dest_filename=dest_filename,
            )
            log_move(item.name, destination, dry_run=dry_run, verbose=verbose)
            mark_as_processed(item)
            stats.moved += 1

        except Exception as e:
            print(f"Error: {item.name} -> {e}")
            stats.errors += 1

    return stats


def organize_all(
    target_dirs: list[Path],
    dry_run:     bool = DRY_RUN,
    model:       str  = OLLAMA_MODEL,
    verbose:     bool = False,
) -> Stats:
    """
    複数フォルダを順番に整理し、Stats を合計して返す。

    - 存在しないフォルダは organize() 内でスキップされる
    - cleanup（Installers / Screenshots）は BASE_DIR に対して 1 回だけ実行する
      （organize() 内の cleanup は無効化し、ここで一元管理する）
    - verbose=True のとき BASE_DIR と処理対象ディレクトリを最初に表示する
    """

    total = Stats()

    # 対象ディレクトリを verbose 表示
    if verbose:
        print(f"INFO Base dir: {BASE_DIR}")
        print("INFO Target dirs:")
        for d in target_dirs:
            print(f"  - {d}")

    # cleanup は BASE_DIR に対して 1 回だけ実行（移動先の整理）
    total.deleted += cleanup_old_installers(
        BASE_DIR, dry_run=dry_run, verbose=verbose
    )
    total.deleted += cleanup_old_screenshots(
        BASE_DIR, dry_run=dry_run, verbose=verbose
    )

    # 各フォルダを整理（cleanup はここで済んでいるので run_cleanup=False）
    for target_dir in target_dirs:
        stats = organize(
            target_dir, dry_run=dry_run, model=model,
            verbose=verbose, run_cleanup=False,
        )
        total.moved   += stats.moved
        total.skipped += stats.skipped
        total.errors  += stats.errors
        # deleted は上の cleanup loop で集計済み

    return total


def watch(
    target_dirs: list[Path],
    dry_run:     bool,
    model:       str,
    verbose:     bool,
) -> None:
    """
    watchdog を使って複数フォルダの変更を監視し、
    新規ファイルが作成されるたびに整理を実行する（常駐モード）。

    - 起動時に BASE_DIR の cleanup_old_installers() / cleanup_old_screenshots() を 1 回実行する
    - 存在しないフォルダは自動スキップする
    - is_file_stable() でダウンロード中ファイルを除外する
    - should_skip_recently_processed() で多重処理を防ぐ
    - 50 イベントごとに cleanup_cache() を実行してメモリを安定させる

    使用するには watchdog が必要:
        pip3 install watchdog
    """

    try:
        from watchdog.events import FileCreatedEvent, FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        print("エラー: watchdog がインストールされていません。")
        print("  pip3 install watchdog")
        raise SystemExit(1)

    # 存在するフォルダのみを対象にする
    active_dirs = [d for d in target_dirs if d.is_dir()]
    if not active_dirs:
        print("エラー: 監視対象フォルダがひとつも存在しません。")
        raise SystemExit(1)

    # 起動時に BASE_DIR の古いインストーラー・スクリーンショットを 1 回だけ掃除する
    print(f"INFO Cleanup start: {BASE_DIR}")
    cleanup_old_installers(BASE_DIR, dry_run=dry_run, verbose=verbose)
    cleanup_old_screenshots(BASE_DIR, dry_run=dry_run, verbose=verbose)

    # イベントカウンター（cleanup_cache の呼び出し頻度制御に使用）
    # list を使って複数ハンドラ間で共有する（クロージャの制約を回避）
    event_counter: list[int] = [0]

    class _OrganizerHandler(FileSystemEventHandler):
        """指定フォルダ直下への新規ファイル作成を監視するハンドラ。"""

        def __init__(self, watched_dir: Path) -> None:
            super().__init__()
            self._watched_dir = watched_dir

        def on_created(self, event: FileCreatedEvent) -> None:  # type: ignore[override]
            if event.is_directory:
                return

            file_path = Path(event.src_path)

            # 監視フォルダ直下のファイルのみ対象（サブフォルダは無視）
            if file_path.parent.resolve() != self._watched_dir.resolve():
                return

            # 50 イベントごとにキャッシュをクリーンアップ（メモリ安定化）
            event_counter[0] += 1
            if event_counter[0] % 50 == 0:
                cleanup_cache(verbose=verbose)

            # スキップ判定（理由付き）
            skip_reason = get_skip_reason(file_path)
            if skip_reason:
                log_skip(skip_reason, file_path.name, verbose)
                return

            # 二重処理チェック（多重イベント防止）
            if should_skip_recently_processed(file_path):
                log_skip("recently processed", file_path.name, verbose)
                return

            # ファイルの書き込み完了を確認（ダウンロード中の部分ファイル対策）
            if not is_file_stable(file_path):
                log_skip("unstable file", file_path.name, verbose)
                return

            try:
                # 安定待機中に削除された場合はスキップ
                if not file_path.exists():
                    return

                category = choose_category(file_path, model=model)
                if not category:
                    return

                if category not in ALLOWED_CATEGORIES:
                    category = "Other"

                # インストーラーは移動先のファイル名を安全な形式へ変換する
                dest_filename = (
                    build_installer_filename(file_path.name)
                    if category == "Installers"
                    else None
                )

                destination = move_file(
                    file_path, category,
                    dest_dir=BASE_DIR,
                    dry_run=dry_run,
                    dest_filename=dest_filename,
                )
                log_move(file_path.name, destination, dry_run=dry_run, verbose=verbose)
                mark_as_processed(file_path)

            except Exception as e:
                print(f"Error: {file_path.name} -> {e}")

    observer = Observer()
    for target_dir in active_dirs:
        observer.schedule(_OrganizerHandler(target_dir), str(target_dir), recursive=False)

    observer.start()
    for d in active_dirs:
        print(f"Watching: {d}  (Ctrl+C で停止)")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
        print("Watch 停止。")


def print_summary(stats: Stats, dry_run: bool = DRY_RUN) -> None:
    print("\n--- 完了 ---")
    if dry_run:
        print("モード: DRY RUN（実際には移動していません）")
    print(f"移動:     {stats.moved}")
    print(f"スキップ: {stats.skipped}")
    print(f"削除:     {stats.deleted}")
    print(f"エラー:   {stats.errors}")


# =========================
# エントリポイント
# =========================


def resolve_target_dirs(args: argparse.Namespace) -> list[Path]:
    """
    CLI 引数から整理対象ディレクトリのリストを解決して返す。

    優先順: --all-dirs > --downloads > --target-dir

    --target-dir に ~ を含む場合も expanduser() で正規化する。
    日本語フォルダ名（デスクトップ / ダウンロード）は
    TARGET_DIR / DOWNLOADS_DIR 定数が既に解決済みのため、
    --all-dirs / --downloads フラグでは定数を参照する。
    """

    if args.all_dirs:
        return list(DEFAULT_TARGET_DIRS)
    if args.downloads:
        return [DOWNLOADS_DIR]
    return [Path(args.target_dir).expanduser()]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Smart Desktop Organizer — ファイルを自動分類して整理します。",
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        default=TARGET_DIR,
        metavar="DIR",
        help=f"整理対象ディレクトリ (デフォルト: {TARGET_DIR})",
    )
    parser.add_argument(
        "--all-dirs",
        action="store_true",
        help=(
            "Desktop と Downloads を両方整理する。"
            f" 対象: {', '.join(str(d) for d in DEFAULT_TARGET_DIRS)}"
        ),
    )
    parser.add_argument(
        "--downloads",
        action="store_true",
        help=f"Downloads フォルダのみを整理する。対象: {DOWNLOADS_DIR}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=DRY_RUN,
        help="実際には移動せず、移動先だけ表示する",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="INFO プレフィックス付きで詳細ログを出力する",
    )
    parser.add_argument(
        "--model",
        default=OLLAMA_MODEL,
        help=f"使用する Ollama モデル名 (デフォルト: {OLLAMA_MODEL})",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="常駐監視モード（watchdog が必要）",
    )
    return parser


if __name__ == "__main__":
    args = _build_parser().parse_args()

    target_dirs: list[Path] = resolve_target_dirs(args)

    if args.watch:
        watch(
            target_dirs=target_dirs,
            dry_run=args.dry_run,
            model=args.model,
            verbose=args.verbose,
        )
    else:
        stats = organize_all(
            target_dirs=target_dirs,
            dry_run=args.dry_run,
            model=args.model,
            verbose=args.verbose,
        )
        print_summary(stats, dry_run=args.dry_run)
