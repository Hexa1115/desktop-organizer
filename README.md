# Smart Desktop Organizer

Desktop と Downloads に置かれたファイルを自動分類し、`~/Documents/DesktopOrganizer/` へ移動する macOS 向け自動整理ツール。

ログイン時・スリープ復帰時に launchd から自動実行される。

---

## 概要

| 項目 | 内容 |
|---|---|
| 監視元 | `~/Desktop`、`~/Downloads`（日本語パス対応） |
| 保存先 | `~/Documents/DesktopOrganizer/<カテゴリ>/` |
| 分類方法 | 拡張子ルール + Ollama（AI）による内容判定 |
| 自動実行 | launchd（ログイン時・スリープ復帰時） |
| ログ | `~/Library/Logs/DesktopOrganizer/organizer.log` |

Desktop / Downloads にカテゴリフォルダは**一切作らない**。

---

## カテゴリ一覧

| カテゴリ | 主な対象 | 自動削除 |
|---|---|---|
| `Installers` | `.dmg` `.pkg` | 7 日経過で削除 |
| `Screenshots` | スクリーンショット画像 | 3 日経過で削除 |
| `Images` | `.jpg` `.png` `.gif` `.webp` `.heic` | なし |
| `PDF` | `.pdf`（内容判定あり） | なし |
| `Document` | PDF の内容が書類と判定された場合 | なし |
| `Archives` | `.zip` `.rar` `.7z` `.tar` `.gz` | なし |
| `Videos` | `.mp4` `.mov` `.mkv` | なし |
| `Audio` | `.mp3` `.wav` `.m4a` | なし |
| `Data` | `.xlsx` `.xls` `.csv`（AI 判定含む） | なし |
| `Code` | `.py` `.js` `.ts` `.html` 等（AI 判定） | なし |
| `Notes` | テキスト系（AI 判定） | なし |
| `Other` / `Unreadable` | 判定できなかったもの | なし |

---

## ファイル構成

```
~/Python/
  smart_organize.py       # 本体
  run_organizer.py        # launchd ラッパー（ロック・ログ管理）
  run_organizer.sh        # 旧ラッパー（現在は未使用）

~/Library/LaunchAgents/
  com.hexa.desktop.organizer.plist  # launchd 設定

~/Library/Logs/DesktopOrganizer/
  organizer.log           # 実行ログ
  launchd_stdout.log      # launchd 標準出力
  launchd_stderr.log      # launchd 標準エラー
```

---

## セットアップ

### 1. 依存関係

```bash
# AI 分類に必要（未インストールでも拡張子ルールで動作する）
brew install ollama
ollama pull llama3

# PDF 内容分類に必要（オプション）
brew install poppler

# watch モードに必要（オプション）
pip3 install watchdog
```

### 2. launchd への登録

```bash
# plist をコピー済みの場合
launchctl load ~/Library/LaunchAgents/com.hexa.desktop.organizer.plist

# 登録確認
launchctl list | grep com.hexa.desktop.organizer
```

### 3. macOS プライバシー設定

**システム設定 → プライバシーとセキュリティ → フルディスクアクセス** に以下を追加：

- `/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3.9`

> launchd から python3.9 を直接呼び出すことで TCC の責任プロセスとして認識させている。bash 経由では権限が付与されない。

---

## 手動実行

```bash
# Desktop + Downloads を整理（本番）
python3 smart_organize.py --all-dirs

# 実際には移動せず確認だけ
python3 smart_organize.py --all-dirs --dry-run --verbose

# Downloads だけ整理
python3 smart_organize.py --downloads

# 特定フォルダを指定
python3 smart_organize.py --target-dir ~/Desktop

# 常駐監視モード（watchdog 必要）
python3 smart_organize.py --all-dirs --watch

# Ollama モデルを指定
python3 smart_organize.py --all-dirs --model llama3
```

---

## launchd の管理

```bash
# 手動で今すぐ実行
launchctl kickstart -k gui/$(id -u)/com.hexa.desktop.organizer

# 停止
launchctl unload ~/Library/LaunchAgents/com.hexa.desktop.organizer.plist

# 再起動
launchctl unload ~/Library/LaunchAgents/com.hexa.desktop.organizer.plist
launchctl load   ~/Library/LaunchAgents/com.hexa.desktop.organizer.plist

# ログをリアルタイム確認
tail -f ~/Library/Logs/DesktopOrganizer/organizer.log
```

---

## 動作仕様

### 実行タイミング

| トリガー | 詳細 |
|---|---|
| ログイン時 | `RunAtLoad` により自動実行 |
| スリープ復帰時 | `IOPMrootDomain` の wake イベントで実行 |
| 60 秒以内の再実行 | `ThrottleInterval=60` により抑制 |

### 安全機能

| 機能 | 説明 |
|---|---|
| ロックファイル | 二重起動を防ぐ（`/tmp/com.hexa.desktop.organizer.lock`） |
| 安定性チェック | ダウンロード中ファイルをスキップ（0.3 秒でサイズ比較） |
| 二重処理防止 | 30 秒以内の同一ファイルはスキップ |
| キャッシュ TTL | 300 秒でキャッシュ自動削除 |
| 同名衝突回避 | `file (1).ext` 形式（macOS Finder 準拠） |
| パストラバーサル防止 | `resolve()` でフォルダ外へのシンボリックリンクを検証 |

### インストーラーのリネーム

移動時に以下の形式へ自動リネーム：

```
Cursor 2.0 Installer.dmg
  → installer_2026-04-03_0930_cursor-2.0-installer.dmg
```

---

## ログの見方

```
INFO Base dir: /Users/hexa/Documents/DesktopOrganizer
INFO Target dirs:
  - /Users/hexa/Desktop
  - /Users/hexa/Downloads
INFO Moved: report.pdf -> /Users/hexa/Documents/DesktopOrganizer/Document/report.pdf
INFO Moved: Claude.dmg -> /Users/hexa/Documents/DesktopOrganizer/Installers/installer_2026-04-03_0930_claude.dmg
SKIP (hidden): .DS_Store
SKIP (directory): Images
SKIP (recently processed): app.dmg
INFO Deleted old installer: /Users/hexa/Documents/DesktopOrganizer/Installers/installer_2026-03-25_1200_old-app.dmg
INFO Deleted old screenshot: /Users/hexa/Documents/DesktopOrganizer/Screenshots/Screen Shot 2026-03-28.png

--- 完了 ---
移動:     5
スキップ: 8
削除:     2
エラー:   0
```
