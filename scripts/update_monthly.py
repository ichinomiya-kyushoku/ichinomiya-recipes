#!/usr/bin/env python3
"""東浅井 月次自動更新スクリプト

毎月20〜26日ごろ一宮市サイトに翌月の給食献立PDFが公開される。
このスクリプトを月1回実行すると：
  1. 未取得の東浅井PDFを自動検出・ダウンロード
  2. data/menu_pdf_list.json に追記
  3. data/menu_days.json を再構築（東浅井全データ）
  4. app/menus.js を再生成
  5. git commit & push (main → gh-pages)

実行方法:
  cd ichinomiya_recipes
  python3 scripts/update_monthly.py

依存: pdfplumber, git（コマンドラインツール）
"""

import json
import re
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIST_JSON   = ROOT / "data" / "menu_pdf_list.json"
DL_DIR      = ROOT / "downloads" / "menus"
MENU_OUT    = ROOT / "data" / "menu_days.json"
MENUS_JS    = ROOT / "app" / "menus.js"

# 東浅井PDFの基本URL（2026年度〜）
BASE_URL = ("https://www.city.ichinomiya.aichi.jp/"
            "_res/projects/default_project/_page_/001/074/863/")

# ──────────────────────────────────────────────────────────────────────────────
# 1. URL・ファイル名を生成
# ──────────────────────────────────────────────────────────────────────────────

def reiwa_year(western: int) -> int:
    return western - 2018


def pdf_filename(year: int, month: int) -> str:
    return f"higashiazaisyoutyuu{reiwa_year(year):02d}{month:02d}.pdf"


def pdf_url(year: int, month: int) -> str:
    return BASE_URL + pdf_filename(year, month)


# ──────────────────────────────────────────────────────────────────────────────
# 2. 未取得月を検出
# ──────────────────────────────────────────────────────────────────────────────

def already_registered(entries, year: int, month: int) -> bool:
    for e in entries:
        if (e.get("area") == "東浅井"
                and e.get("year") == year
                and e.get("month") == month):
            return True
    return False


def months_to_check(today: date):
    """今月と翌月のうち、まだ登録されていない可能性があるものを返す。"""
    result = []
    # 当月 + 翌月を確認する
    for delta in range(2):
        m = today.month + delta
        y = today.year
        if m > 12:
            m -= 12
            y += 1
        result.append((y, m))
    return result


# ──────────────────────────────────────────────────────────────────────────────
# 3. HTTPダウンロード
# ──────────────────────────────────────────────────────────────────────────────

def try_download(url: str, dest: Path) -> bool:
    """URLからダウンロードを試みる。成功したらTrueを返す。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                return False
            data = resp.read()
            if len(data) < 1000:          # PDFとしては小さすぎる（エラーページ等）
                return False
            dest.write_bytes(data)
            return True
    except (urllib.error.URLError, OSError):
        return False


# ──────────────────────────────────────────────────────────────────────────────
# 4. パーサー（parse_menus.py と同じロジックをインライン）
# ──────────────────────────────────────────────────────────────────────────────

def _parse_menus():
    """data/menu_pdf_list.json 全エントリを再解析して menu_days.json を更新する。"""
    # parse_menus.py を subprocess で呼ぶ（コードの二重管理を避ける）
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "parse_menus.py")],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print("=== parse_menus.py stderr ===")
        print(result.stderr)
        raise RuntimeError("parse_menus.py が失敗しました")
    print(result.stdout.strip())


def _build_menus_js():
    """data/menu_days.json から app/menus.js を再生成する。"""
    menus = json.loads(MENU_OUT.read_text(encoding="utf-8"))
    js = "window.MENUS = " + json.dumps(menus, ensure_ascii=False) + ";\n"
    MENUS_JS.write_text(js, encoding="utf-8")
    print(f"menus.js 書き込み完了: {len(menus)} 件 ({MENUS_JS.stat().st_size // 1024} KB)")


# ──────────────────────────────────────────────────────────────────────────────
# 5. git commit & push（main → gh-pages）
# ──────────────────────────────────────────────────────────────────────────────

def _run(cmd: list[str], **kwargs):
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        print(f"コマンド失敗: {' '.join(cmd)}")
        print(result.stderr)
        raise RuntimeError(f"git コマンドが失敗: {' '.join(cmd)}")
    return result.stdout.strip()


def git_commit_and_push(added_entries: list[dict]):
    """変更を main にコミットし、gh-pages にも menus.js を反映する。"""
    months_desc = ", ".join(
        f"{e['year']}年{e['month']}月" for e in added_entries
    )

    # ── main ブランチにコミット ─────────────────────────────
    _run(["git", "add",
          str(LIST_JSON.relative_to(ROOT)),
          str(MENU_OUT.relative_to(ROOT)),
          str(MENUS_JS.relative_to(ROOT))],
         cwd=ROOT)

    msg = f"feat: 東浅井給食献立を追加 ({months_desc})\n\n自動更新スクリプトによる取り込み"
    _run(["git", "commit", "-m", msg], cwd=ROOT)
    _run(["git", "push", "origin", "main"], cwd=ROOT)
    print("main へのプッシュ完了")

    # ── gh-pages ブランチに menus.js をコピー ──────────────
    # main の menus.js 内容を tmp に退避してから gh-pages へ
    tmp = Path("/tmp/menus_monthly.js")
    shutil.copy(MENUS_JS, tmp)

    _run(["git", "checkout", "gh-pages"], cwd=ROOT)

    # gh-pages 上の作業ディレクトリにコピー
    dest = ROOT / "menus.js"        # gh-pages では root 直下
    shutil.copy(tmp, dest)

    _run(["git", "add", "menus.js"], cwd=ROOT)
    _run(["git", "commit", "-m", f"deploy: menus.js 更新 ({months_desc})"], cwd=ROOT)
    _run(["git", "push", "origin", "gh-pages"], cwd=ROOT)
    print("gh-pages へのプッシュ完了")

    # main に戻す
    _run(["git", "checkout", "main"], cwd=ROOT)
    print("main ブランチに戻りました")


# ──────────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    today = date.today()
    print(f"=== 東浅井 月次更新チェック（{today}）===")

    entries: list[dict] = json.loads(LIST_JSON.read_text(encoding="utf-8"))
    DL_DIR.mkdir(parents=True, exist_ok=True)

    added = []

    for year, month in months_to_check(today):
        if already_registered(entries, year, month):
            print(f"  {year}年{month}月: 登録済み → スキップ")
            continue

        fname = pdf_filename(year, month)
        url   = pdf_url(year, month)
        dest  = DL_DIR / fname

        print(f"  {year}年{month}月: {fname} を確認中...")

        if dest.exists():
            print(f"    ファイルは既にダウンロード済み。エントリのみ追加します。")
            downloaded = True
        else:
            downloaded = try_download(url, dest)

        if downloaded:
            print(f"    ダウンロード成功: {dest}")
            entry = {
                "url": url,
                "filename": fname,
                "school_type": "小中",
                "area": "東浅井",
                "year": year,
                "month": month,
                "year_month": f"{year}-{month:02d}",
            }
            entries.append(entry)
            added.append(entry)
        else:
            print(f"    まだ公開されていないか、URLが変更された可能性があります。")

    if not added:
        print("\n新規追加なし。終了します。")
        return

    # JSON 更新
    LIST_JSON.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"\nmenu_pdf_list.json を更新しました（+{len(added)} エントリ）")

    # 解析 → menus.js 再生成
    print("\nPDFを解析しています...")
    _parse_menus()
    print("menus.js を再生成しています...")
    _build_menus_js()

    # git コミット & プッシュ
    print("\ngit commit & push...")
    git_commit_and_push(added)

    added_labels = ', '.join('{}年{}月'.format(e['year'], e['month']) for e in added)
    print(f"\n✓ 完了: {added_labels} を追加しました")


if __name__ == "__main__":
    main()
