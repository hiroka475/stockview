#!/usr/bin/env python3
"""
スクリーニングデータ検証スクリプト
screening-cache.json のデータを yfinance で再取得した値と比較する

使い方:
  pip3 install yfinance
  python3 verify-screening.py        # デフォルト5銘柄
  python3 verify-screening.py 20     # 20銘柄を検証
"""

import json
import time
import sys

try:
    import yfinance as yf
except ImportError:
    print("yfinanceが必要です。以下を実行してください:")
    print("  pip3 install yfinance")
    sys.exit(1)

# 検証する銘柄数（引数で指定可能）
NUM_STOCKS = int(sys.argv[1]) if len(sys.argv) > 1 else 5


def fetch_yahoo_data(code: str) -> dict:
    """yfinanceから検証用データを取得"""
    symbol = f"{code}.T"
    result = {}

    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info

        if not info or info.get("regularMarketPrice") is None:
            return {"error": "データ取得不可"}

        result["price"] = info.get("regularMarketPrice") or info.get("currentPrice")
        result["dividendYield"] = info.get("dividendYield")
        if result["dividendYield"]:
            result["dividendYield"] = round(result["dividendYield"] * 100, 2)
        result["trailingPE"] = info.get("trailingPE")
        result["forwardPE"] = info.get("forwardPE")
        result["priceToBook"] = info.get("priceToBook")
        result["dividendRate"] = info.get("dividendRate")
        result["trailingEps"] = info.get("trailingEps")

        # 営業利益率
        om = info.get("operatingMargins")
        if om:
            result["operatingMargins"] = round(om * 100, 2)

        # ROE
        roe = info.get("returnOnEquity")
        if roe:
            result["returnOnEquity"] = round(roe * 100, 2)

    except Exception as e:
        result["error"] = str(e)

    return result


def compare_values(label, cached, yahoo, tolerance_pct=10):
    """
    2つの値を比較して結果を返す
    tolerance_pct: 許容誤差（%）
    """
    if cached is None and yahoo is None:
        return "⬜ 両方なし", None
    if cached is None:
        return f"⚠️ キャッシュなし (Yahoo={yahoo})", None
    if yahoo is None:
        return f"⚠️ Yahoo取得不可 (キャッシュ={cached})", None

    if yahoo == 0:
        if cached == 0:
            return "✅ 一致 (0)", 0
        return f"❌ 不一致 (キャッシュ={cached}, Yahoo=0)", None

    diff_pct = abs(cached - yahoo) / abs(yahoo) * 100

    if diff_pct <= tolerance_pct:
        return f"✅ 一致 ({cached} vs {yahoo}, 差{diff_pct:.1f}%)", diff_pct
    elif diff_pct <= 30:
        return f"⚠️ やや乖離 ({cached} vs {yahoo}, 差{diff_pct:.1f}%)", diff_pct
    else:
        return f"❌ 大きな乖離 ({cached} vs {yahoo}, 差{diff_pct:.1f}%)", diff_pct


def main():
    # screening-cache.json を読み込む
    with open("screening-cache.json", "r", encoding="utf-8") as f:
        cache = json.load(f)

    codes = list(cache.keys())[:NUM_STOCKS]

    print(f"=" * 70)
    print(f"スクリーニングデータ検証レポート")
    print(f"検証銘柄数: {len(codes)}")
    print(f"比較元: screening-cache.json vs yfinance")
    print(f"=" * 70)

    all_results = []

    for i, code in enumerate(codes):
        cached = cache[code]
        name = cached.get("name", "不明")
        print(f"\n{'─' * 50}")
        print(f"[{i+1}/{len(codes)}] {code} {name}")
        print(f"{'─' * 50}")

        yahoo = fetch_yahoo_data(code)

        if "error" in yahoo:
            print(f"  ⛔ Yahoo Finance取得エラー: {yahoo['error']}")
            all_results.append((code, name, []))
            continue

        comparisons = [
            ("株価", cached.get("price"), yahoo.get("price"), 5),
            ("配当利回り(%)", cached.get("dividendYield"), yahoo.get("dividendYield"), 15),
            ("PER", cached.get("per"), yahoo.get("trailingPE") or yahoo.get("forwardPE"), 15),
            ("PBR", cached.get("pbr"), yahoo.get("priceToBook"), 15),
            ("EPS", cached.get("eps"), yahoo.get("trailingEps"), 15),
            ("営業利益率(%)", cached.get("operatingMargin"), yahoo.get("operatingMargins"), 20),
            ("ROE(%)", cached.get("roe"), yahoo.get("returnOnEquity"), 20),
            ("年間配当金", cached.get("dividendAnnual"), yahoo.get("dividendRate"), 15),
        ]

        stock_results = []
        for label, cv, yv, tol in comparisons:
            result_str, diff = compare_values(label, cv, yv, tol)
            print(f"  {label:15s}: {result_str}")
            stock_results.append((label, cv, yv, diff, result_str))

        all_results.append((code, name, stock_results))

        if i < len(codes) - 1:
            time.sleep(1)  # API負荷軽減

    # サマリー
    print(f"\n{'=' * 70}")
    print("サマリー")
    print(f"{'=' * 70}")

    ok_count = 0
    warn_count = 0
    ng_count = 0
    na_count = 0
    error_count = 0

    for code, name, results in all_results:
        if not results:
            error_count += 1
            continue
        for label, cv, yv, diff, result_str in results:
            if "✅" in result_str:
                ok_count += 1
            elif "⚠️" in result_str:
                warn_count += 1
            elif "❌" in result_str:
                ng_count += 1
            else:
                na_count += 1

    total = ok_count + warn_count + ng_count + na_count
    print(f"  ✅ 一致:       {ok_count}/{total}")
    print(f"  ⚠️ やや乖離:   {warn_count}/{total}")
    print(f"  ❌ 大きな乖離: {ng_count}/{total}")
    print(f"  ⬜ データなし:  {na_count}/{total}")
    if error_count:
        print(f"  ⛔ 取得エラー: {error_count}銘柄")

    if total > 0:
        accuracy = (ok_count / total) * 100
        print(f"\n  正確率: {accuracy:.1f}%")

    # 大きな乖離がある項目を一覧表示
    print(f"\n{'─' * 50}")
    print("要確認項目（大きな乖離）:")
    print(f"{'─' * 50}")
    found = False
    for code, name, results in all_results:
        for label, cv, yv, diff, result_str in results:
            if "❌" in result_str:
                print(f"  {code} {name} - {label}: キャッシュ={cv}, Yahoo={yv}")
                found = True
    if not found:
        print("  なし（すべて許容範囲内）")


if __name__ == "__main__":
    main()
