#!/usr/bin/env python3
"""
スクリーニングデータの事前バッチ取得スクリプト

毎日1回（例: 早朝5時）に実行して、全候補銘柄のデータを
IR BANK + Yahoo Finance から取得し screening-cache.json に保存する。

これにより、ユーザーがスクリーニング画面を開いた際は
キャッシュファイルからデータを即座に返し、IR BANKへの
リアルタイムアクセスを不要にする。

使い方:
  python3 update-screening-data.py              # 全銘柄取得
  python3 update-screening-data.py --market プライム  # プライムのみ
  python3 update-screening-data.py --limit 10   # 最初の10銘柄のみ（テスト用）

定期実行（macOS の場合）:
  crontab -e で以下を追加:
  0 5 * * * cd /Users/ogurahirokazu/claude-code-lab/stock-view && python3 update-screening-data.py >> update-screening.log 2>&1
"""

import argparse
import json
import os
import sys
import time

# server.py と同じディレクトリにあることを前提
_base_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _base_dir)

# server.py の関数をインポート
# ※ server.py が HTTPServer を起動しないよう、関数だけ使う
print("=" * 60)
print(f"スクリーニングデータ バッチ更新")
print(f"開始時刻: {time.strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="スクリーニングデータの事前バッチ取得")
    parser.add_argument("--market", type=str, default="", help="市場区分フィルター（例: プライム）")
    parser.add_argument("--limit", type=int, default=0, help="取得銘柄数の上限（0=全件）")
    parser.add_argument("--interval", type=float, default=3.0, help="IR BANKアクセス間隔（秒）")
    args = parser.parse_args()

    # 候補銘柄リストを読み込む
    candidates_path = os.path.join(_base_dir, "dividend-candidates.json")
    if not os.path.exists(candidates_path):
        print(f"エラー: {candidates_path} が見つかりません。")
        print("先に expand-stocks.py を実行してください。")
        sys.exit(1)

    with open(candidates_path, "r", encoding="utf-8") as f:
        candidates = json.load(f)
    print(f"候補銘柄: {len(candidates)}銘柄")

    # 市場区分フィルター
    if args.market:
        candidates = [c for c in candidates if args.market in c.get("market", "")]
        print(f"市場区分フィルター '{args.market}': {len(candidates)}銘柄")

    # 上限
    if args.limit > 0:
        candidates = candidates[:args.limit]
        print(f"上限指定: {len(candidates)}銘柄に制限")

    codes = [c["code"] for c in candidates]

    # 既存キャッシュを読み込む
    cache_path = os.path.join(_base_dir, "screening-cache.json")
    existing_cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                existing_cache = json.load(f)
            print(f"既存キャッシュ: {len(existing_cache)}銘柄")
        except Exception as e:
            print(f"既存キャッシュ読み込みエラー: {e}")

    # server.py の関数をインポート
    try:
        from server import (
            fetch_dividend_data,
            fetch_fundamentals_data,
            _get_latest_value,
            _get_latest_value_with_type,
            _calc_growth_rate,
            _calc_consecutive_dividend_years,
            _IRBANK_ACCESS_INTERVAL,
        )
    except ImportError as e:
        print(f"server.py のインポートエラー: {e}")
        print("server.py と同じディレクトリで実行してください。")
        sys.exit(1)

    # 1銘柄ずつ取得（並列にしない＝IR BANKに優しい）
    results = dict(existing_cache)  # 既存キャッシュをベースにする
    success_count = 0
    error_count = 0
    skip_count = 0
    start_time = time.time()

    for i, code in enumerate(codes):
        progress = f"[{i+1}/{len(codes)}]"

        # 既存キャッシュが30日以内なら株価だけ更新（IR BANKアクセスなし）
        existing = existing_cache.get(code, {})
        last_updated = existing.get("lastUpdated", 0)
        cache_age_hours = (time.time() - last_updated) / 3600 if last_updated else 999

        if cache_age_hours < 720 and existing.get("dividendType"):  # 30日 = 720時間
            # 30日以内のキャッシュがある → 株価だけ更新
            try:
                symbol = f"{code}.T"
                div_data = fetch_dividend_data(symbol)
                price = div_data.get("price", 0)
                if price and price > 0:
                    existing["price"] = price
                    # 配当利回り再計算
                    div_annual = existing.get("dividendAnnual", 0)
                    if div_annual and div_annual > 0:
                        existing["dividendYield"] = round(div_annual / price * 100, 2)
                    # PER/PBR再計算
                    eps = existing.get("eps")
                    if eps and eps > 0:
                        existing["per"] = round(price / eps, 2)
                    bps = existing.get("bps")
                    if bps and bps > 0:
                        existing["pbr"] = round(price / bps, 2)
                    existing["lastUpdated"] = int(time.time())
                    results[code] = existing
                    skip_count += 1
                    print(f"  {progress} {code}: 株価更新のみ（¥{price}、キャッシュ {cache_age_hours:.1f}h）")
                    continue
            except Exception as e:
                print(f"  {progress} {code}: 株価更新失敗 ({e})")

        # フル取得
        print(f"  {progress} {code}: フル取得開始...")
        result = {"code": code, "name": "", "sector": ""}

        # マスタデータ
        for c in candidates:
            if c["code"] == code:
                result["name"] = c["name"]
                result["sector"] = c["sector"]
                break

        # Yahoo Finance から株価
        try:
            symbol = f"{code}.T"
            div_data = fetch_dividend_data(symbol)
            result["price"] = div_data.get("price", 0)
        except Exception as e:
            print(f"    株価取得エラー: {e}")

        # IR BANKアクセス間隔を守る
        time.sleep(args.interval)

        # IR BANK からファンダメンタルズ
        try:
            funda = fetch_fundamentals_data(code)

            if funda.get("error"):
                print(f"    IR BANKエラー: {funda['error']}")
                error_count += 1
                # エラー時は既存キャッシュを維持
                if code in existing_cache:
                    results[code] = existing_cache[code]
                continue

            sections = funda.get("sections", {})
            periods = funda.get("periods", [])

            if not sections:
                print(f"    データ空（アクセス制限の可能性）")
                error_count += 1
                if code in existing_cache:
                    results[code] = existing_cache[code]
                continue

            # 配当
            dividend_val, dividend_type = _get_latest_value_with_type(
                sections.get("dividend", {}), periods
            )
            result["dividendAnnual"] = dividend_val
            result["dividendType"] = dividend_type

            # 配当利回り
            price = result.get("price", 0)
            if price and price > 0 and dividend_val and dividend_val > 0:
                result["dividendYield"] = round(dividend_val / price * 100, 2)
            else:
                result["dividendYield"] = 0

            # EPS
            eps_val, eps_type = _get_latest_value_with_type(
                sections.get("eps", {}), periods
            )
            result["eps"] = eps_val
            result["epsType"] = eps_type

            # BPS
            bps_val, bps_type = _get_latest_value_with_type(
                sections.get("bps", {}), periods
            )
            result["bps"] = bps_val
            result["bpsType"] = bps_type

            # PER
            if price and price > 0 and eps_val and eps_val > 0:
                result["per"] = round(price / eps_val, 2)
                result["perType"] = eps_type

            # PBR
            if price and price > 0 and bps_val and bps_val > 0:
                result["pbr"] = round(price / bps_val, 2)
                result["pbrType"] = bps_type

            # その他
            result["operatingMargin"] = _get_latest_value(sections.get("operatingMargin", {}))
            result["roe"] = _get_latest_value(sections.get("roe", {}))
            result["equityRatio"] = _get_latest_value(sections.get("equityRatio", {}))
            result["payoutRatio"] = _get_latest_value(sections.get("payoutRatio", {}))
            result["revenueGrowth1y"] = _calc_growth_rate(sections.get("revenue", {}))
            result["consecutiveDividendYears"] = _calc_consecutive_dividend_years(
                sections.get("dividend", {})
            )

            result["lastUpdated"] = int(time.time())
            results[code] = result
            success_count += 1
            print(f"    OK: 配当={dividend_val}({dividend_type}), 利回り={result.get('dividendYield')}%")

        except Exception as e:
            print(f"    ファンダメンタルズ取得エラー: {e}")
            error_count += 1
            if code in existing_cache:
                results[code] = existing_cache[code]

        # 5銘柄ごとに途中保存（中断しても失われないように）
        if (i + 1) % 5 == 0:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False)

    # 最終保存
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False)

    elapsed = time.time() - start_time
    print()
    print("=" * 60)
    print(f"バッチ更新完了")
    print(f"  フル取得: {success_count}銘柄")
    print(f"  株価のみ更新: {skip_count}銘柄")
    print(f"  エラー: {error_count}銘柄")
    print(f"  合計キャッシュ: {len(results)}銘柄")
    print(f"  所要時間: {elapsed:.0f}秒（{elapsed/60:.1f}分）")
    print(f"  保存先: {cache_path}")
    print(f"終了時刻: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)


if __name__ == "__main__":
    main()
