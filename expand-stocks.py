#!/usr/bin/env python3
"""
東証プライム・スタンダード市場の全銘柄リストを取得するスクリプト

JPXの公式データ（東証上場銘柄一覧）をダウンロードし、
jp-stocks.json と dividend-candidates.json を更新します。

使い方:
  cd stock-view
  pip3 install pandas openpyxl xlrd
  python3 expand-stocks.py
"""

import json
import os
import re
import ssl
import sys
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# JPXの東証上場銘柄一覧ダウンロードURL
JPX_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"

# macOSのPythonでSSL証明書エラーを回避
ssl_context = ssl.create_default_context()
try:
    import certifi
    ssl_context.load_verify_locations(certifi.where())
except ImportError:
    # certifiがない場合は証明書検証を無効化（開発用）
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE


def download_jpx_file():
    """JPXから銘柄一覧ファイルをダウンロード"""
    print("JPXから銘柄リストをダウンロード中...")
    req = urllib.request.Request(JPX_URL, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    with urllib.request.urlopen(req, timeout=30, context=ssl_context) as resp:
        data = resp.read()
    print(f"  ダウンロード完了 ({len(data):,} bytes)")
    return data


def parse_with_pandas(raw_data):
    """pandasを使ってExcelファイルをパース"""
    import pandas as pd
    import io

    # .xlsファイルをpandasで読み込み（xlrdが必要）
    try:
        df = pd.read_excel(io.BytesIO(raw_data), engine="xlrd")
    except Exception:
        # xlrdが使えない場合、HTML形式として試す
        df = pd.read_html(io.BytesIO(raw_data))[0]

    print(f"  読み込み: {len(df)}行, カラム: {list(df.columns)}")

    # カラム名を正規化して探す
    # JPXのカラム: 日付, コード, 銘柄名, 市場・商品区分, 33業種区分, 33業種コード, 17業種コード, 17業種区分, 規模コード, 規模区分
    col_map = {}
    for col in df.columns:
        col_str = str(col).strip()
        if col_str == "コード":
            col_map["code"] = col
        elif "銘柄名" in col_str or "会社名" in col_str:
            col_map["name"] = col
        elif "市場" in col_str:
            col_map["market"] = col
        elif col_str == "33業種区分":
            col_map["sector"] = col
        elif "規模" in col_str and "コード" not in col_str:
            col_map["size"] = col

    # カラム名が日本語でない場合の対応
    if not col_map:
        cols = list(df.columns)
        if len(cols) >= 4:
            # 典型的な順序: 日付, コード, 銘柄名, 市場..., 業種
            for i, col in enumerate(cols):
                sample = str(df[col].iloc[0]) if len(df) > 0 else ""
                if re.match(r'^\d{4,5}$', sample):
                    col_map["code"] = col
                elif i > 0 and "code" in col_map and "name" not in col_map:
                    col_map["name"] = col

    if "code" not in col_map or "name" not in col_map:
        print(f"  カラムマッピング失敗: {col_map}")
        print(f"  利用可能なカラム: {list(df.columns)}")
        return []

    print(f"  カラムマッピング: {col_map}")

    stocks = []
    for _, row in df.iterrows():
        code = str(row.get(col_map["code"], "")).strip()
        name = str(row.get(col_map["name"], "")).strip()
        market = str(row.get(col_map.get("market", ""), "")).strip() if "market" in col_map else ""
        sector = str(row.get(col_map.get("sector", ""), "")).strip() if "sector" in col_map else ""
        size = str(row.get(col_map.get("size", ""), "")).strip() if "size" in col_map else ""
        # 「-」や「nan」を空文字に正規化
        if size in ("-", "nan", "NaN"):
            size = ""

        # 4桁 or 5桁の数字コードのみ
        code = code.replace(".0", "")  # pandasが数値として読む場合
        if re.match(r'^\d{4,5}$', code) and name and name != "nan":
            stocks.append({
                "code": code,
                "name": name,
                "market": market,
                "sector": sector,
                "size": size,
            })

    return stocks


def parse_html_fallback(raw_data):
    """HTML形式のxlsをregexでパース（pandas不要の代替方法）"""
    # エンコーディング検出
    for enc in ["cp932", "shift_jis", "utf-8", "utf-8-sig"]:
        try:
            text = raw_data.decode(enc)
            break
        except (UnicodeDecodeError, Exception):
            continue
    else:
        return []

    if "<table" not in text.lower() and "<tr" not in text.lower():
        return []

    stocks = []
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', text, re.DOTALL | re.IGNORECASE)
    header_idx = {}

    for i, row in enumerate(rows):
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.DOTALL | re.IGNORECASE)
        cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
        if not cells:
            continue

        joined = "".join(cells)
        if i < 5 and ("コード" in joined or "銘柄名" in joined):
            for j, c in enumerate(cells):
                if "コード" in c:
                    header_idx["code"] = j
                elif "銘柄名" in c or "会社名" in c:
                    header_idx["name"] = j
                elif "市場" in c and "商品" not in c:
                    header_idx["market"] = j
                elif "業種" in c:
                    header_idx["sector"] = j
            continue

        if not header_idx or "code" not in header_idx:
            continue

        code = cells[header_idx["code"]] if header_idx.get("code", -1) < len(cells) else ""
        name = cells[header_idx["name"]] if header_idx.get("name", -1) < len(cells) else ""
        market = cells[header_idx.get("market", -1)] if header_idx.get("market", -1) < len(cells) else ""
        sector = cells[header_idx.get("sector", -1)] if header_idx.get("sector", -1) < len(cells) else ""

        if re.match(r'^\d{4,5}$', code) and name:
            stocks.append({"code": code, "name": name, "market": market, "sector": sector})

    return stocks


def filter_prime_standard(stocks):
    """プライム・スタンダード市場の銘柄だけを抽出"""
    target_markets = {"プライム", "スタンダード", "Prime", "Standard",
                      "プライム（内国株式）", "スタンダード（内国株式）"}

    filtered = [s for s in stocks if s.get("market") in target_markets]

    if not filtered:
        # 部分一致でも試す
        filtered = [s for s in stocks
                    if "プライム" in s.get("market", "") or "スタンダード" in s.get("market", "")]

    if not filtered:
        print("  ⚠ 市場区分でフィルタできませんでした。ETF/REITを除外して全銘柄を使います。")
        etf_words = {"ETF", "REIT", ""}
        filtered = [s for s in stocks if s.get("sector", "") not in etf_words]

    return filtered


def save_json_files(stocks):
    """jp-stocks.json と dividend-candidates.json を保存"""
    stocks.sort(key=lambda x: x["code"])

    # jp-stocks.json（market, size含む）
    jp_stocks = [{"code": s["code"], "name": s["name"], "sector": s.get("sector", ""), "market": s.get("market", ""), "size": s.get("size", "")} for s in stocks]

    # dividend-candidates.json（ETF/REIT除外、market含む）
    etf_keywords = ["ETF", "REIT", "上場投資", "インデックス", "ファンド",
                    "ブル", "ベア", "レバレッジ", "インバース", "ダブル"]
    candidates = [
        s for s in jp_stocks
        if s["sector"] not in ("ETF", "REIT")
        and not any(kw in s["name"] for kw in etf_keywords)
    ]

    jp_path = os.path.join(SCRIPT_DIR, "jp-stocks.json")
    cand_path = os.path.join(SCRIPT_DIR, "dividend-candidates.json")

    # バックアップ
    for path in [jp_path, cand_path]:
        if os.path.exists(path):
            bak = path + ".bak"
            if os.path.exists(bak):
                os.remove(bak)
            os.rename(path, bak)
            print(f"  バックアップ: {os.path.basename(bak)}")

    with open(jp_path, "w", encoding="utf-8") as f:
        json.dump(jp_stocks, f, ensure_ascii=False, indent=2)
    print(f"  保存: jp-stocks.json ({len(jp_stocks)}銘柄)")

    with open(cand_path, "w", encoding="utf-8") as f:
        json.dump(candidates, f, ensure_ascii=False, indent=2)
    print(f"  保存: dividend-candidates.json ({len(candidates)}銘柄)")

    # スクリーニングキャッシュを削除（古いデータが混ざるのを防止）
    cache_path = os.path.join(SCRIPT_DIR, "screening-cache.json")
    if os.path.exists(cache_path):
        os.remove(cache_path)
        print(f"  スクリーニングキャッシュを削除しました（次回アクセス時に再取得されます）")

    # セクター別集計
    sectors = {}
    for s in candidates:
        sec = s.get("sector") or "不明"
        sectors[sec] = sectors.get(sec, 0) + 1
    print(f"\n--- セクター別集計（上位10） ---")
    for sec, count in sorted(sectors.items(), key=lambda x: -x[1])[:10]:
        print(f"  {sec}: {count}銘柄")


def main():
    print("=" * 60)
    print(" 東証プライム・スタンダード 銘柄リスト取得ツール")
    print("=" * 60)
    print()

    # Step 1: JPXファイルをダウンロード
    try:
        raw_data = download_jpx_file()
    except Exception as e:
        print(f"  ダウンロードエラー: {e}")
        print("\n手動でダウンロードしてください:")
        print(f"  URL: {JPX_URL}")
        print("  ダウンロード後、このスクリプトと同じフォルダに data_j.xls として保存")
        # ローカルファイルがあるかチェック
        local_path = os.path.join(SCRIPT_DIR, "data_j.xls")
        if os.path.exists(local_path):
            print(f"\n  ローカルファイル発見: {local_path}")
            with open(local_path, "rb") as f:
                raw_data = f.read()
        else:
            sys.exit(1)

    # Step 2: パース
    print("\n銘柄データを解析中...")
    stocks = []

    # pandas方式を試す
    try:
        import pandas
        stocks = parse_with_pandas(raw_data)
    except ImportError:
        print("  pandasがインストールされていません。")
        print("  → pip3 install pandas xlrd")
        print("  HTML解析を試みます...")
        stocks = parse_html_fallback(raw_data)
    except Exception as e:
        print(f"  pandas解析エラー: {e}")
        print("  HTML解析を試みます...")
        stocks = parse_html_fallback(raw_data)

    if not stocks:
        print("\n銘柄データを取得できませんでした。")
        print("以下を確認してください:")
        print("  1. pip3 install pandas xlrd を実行")
        print("  2. もう一度 python3 expand-stocks.py を実行")
        sys.exit(1)

    print(f"  全銘柄: {len(stocks)}")

    # Step 3: プライム+スタンダードに絞る
    print("\nプライム・スタンダード市場をフィルタ中...")
    filtered = filter_prime_standard(stocks)
    print(f"  対象銘柄: {len(filtered)}")

    # Step 4: 保存
    print("\nJSONファイルを保存中...")
    save_json_files(filtered)

    print("\n" + "=" * 60)
    print(" 完了！スクリーニングで使える銘柄数が増えました。")
    print(" サーバーを再起動してください: python3 server.py")
    print("=" * 60)


if __name__ == "__main__":
    main()
