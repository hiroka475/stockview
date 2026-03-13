#!/usr/bin/env python3
"""
スクリーニングデータの事前バッチ取得スクリプト

3つのモードで実行可能:

  python3 update-screening-data.py --mode yahoo     # Yahoo Financeのみ（株価・利回り・PER・PBR更新）
  python3 update-screening-data.py --mode irbank    # IR BANKの古い順に500銘柄（決算データ更新）
  python3 update-screening-data.py --mode full      # 両方（従来動作）

オプション:
  --market プライム    市場区分で絞り込み
  --limit 10          銘柄数上限（テスト用）
  --batch-size 500    IR BANKモードの1回あたり取得数（デフォルト500）
  --interval 3.0      IR BANKアクセス間隔（秒）

定期実行の推奨設定（macOS crontab）:
  # 毎週日曜 5:00 に株価更新（約26分）
  0 5 * * 0 cd /Users/ogurahirokazu/claude-code-lab/stock-view && python3 update-screening-data.py --mode yahoo >> update-screening.log 2>&1
  # 毎日 5:30 にIR BANK 500銘柄ローテーション（約25分）
  30 5 * * * cd /Users/ogurahirokazu/claude-code-lab/stock-view && python3 update-screening-data.py --mode irbank >> update-screening.log 2>&1
"""

import argparse
import json
import os
import sys
import time

_base_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _base_dir)


def load_candidates(market_filter=""):
    """候補銘柄リストを読み込む"""
    candidates_path = os.path.join(_base_dir, "dividend-candidates.json")
    if not os.path.exists(candidates_path):
        print(f"エラー: {candidates_path} が見つかりません。")
        sys.exit(1)

    with open(candidates_path, "r", encoding="utf-8") as f:
        candidates = json.load(f)

    if market_filter:
        candidates = [c for c in candidates if market_filter in c.get("market", "")]
        print(f"市場区分フィルター '{market_filter}': {len(candidates)}銘柄")

    return candidates


def load_cache():
    """既存キャッシュを読み込む"""
    cache_path = os.path.join(_base_dir, "screening-cache.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"既存キャッシュ読み込みエラー: {e}")
    return {}


def save_cache(cache):
    """キャッシュを保存"""
    cache_path = os.path.join(_base_dir, "screening-cache.json")
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def import_server_functions():
    """server.pyから必要な関数をインポート"""
    try:
        from server import (
            fetch_dividend_data,
            fetch_fundamentals_data,
            _get_latest_value_with_type,
        )
        return {
            "fetch_dividend_data": fetch_dividend_data,
            "fetch_fundamentals_data": fetch_fundamentals_data,
            "_get_latest_value_with_type": _get_latest_value_with_type,
        }
    except ImportError as e:
        print(f"server.py のインポートエラー: {e}")
        print("server.py と同じディレクトリで実行してください。")
        sys.exit(1)


def mode_yahoo(candidates, cache, limit=0):
    """Yahoo Financeから株価のみ更新（IR BANKアクセスなし・高速）"""
    funcs = import_server_functions()
    fetch_dividend_data = funcs["fetch_dividend_data"]

    codes = [c["code"] for c in candidates]
    if limit > 0:
        codes = codes[:limit]

    # キャッシュにある銘柄のみ更新（キャッシュにない銘柄は株価だけでは意味がない）
    target_codes = [c for c in codes if c in cache]
    print(f"Yahoo Finance 株価更新: {len(target_codes)}銘柄（キャッシュ済みのみ）")

    updated = 0
    errors = 0
    start_time = time.time()

    for i, code in enumerate(target_codes):
        progress = f"[{i+1}/{len(target_codes)}]"
        try:
            symbol = f"{code}.T"
            div_data = fetch_dividend_data(symbol)
            price = div_data.get("price", 0)
            if not price or price <= 0:
                print(f"  {progress} {code}: 株価取得失敗")
                errors += 1
                continue

            existing = cache[code]
            existing["price"] = price

            # 再計算
            div_annual = existing.get("dividendAnnual")
            if div_annual and div_annual > 0:
                existing["dividendYield"] = round(div_annual / price * 100, 2)
            eps = existing.get("eps")
            if eps and eps > 0:
                existing["per"] = round(price / eps, 2)
            bps = existing.get("bps")
            if bps and bps > 0:
                existing["pbr"] = round(price / bps, 2)

            existing["lastUpdated_yahoo"] = int(time.time())
            existing["lastUpdated"] = int(time.time())
            cache[code] = existing
            updated += 1

            if (i + 1) % 50 == 0:
                elapsed = time.time() - start_time
                print(f"  {progress} {code}: ¥{price} ({elapsed:.0f}秒経過)")
                save_cache(cache)  # 途中保存

        except Exception as e:
            print(f"  {progress} {code}: エラー ({e})")
            errors += 1

    save_cache(cache)
    elapsed = time.time() - start_time
    print(f"\nYahoo Finance更新完了: {updated}銘柄更新, {errors}エラー ({elapsed:.0f}秒)")
    return cache


def mode_irbank(candidates, cache, batch_size=500, interval=3.0, limit=0):
    """IR BANKの古い順にbatch_size銘柄を更新"""
    funcs = import_server_functions()
    fetch_dividend_data = funcs["fetch_dividend_data"]
    fetch_fundamentals_data = funcs["fetch_fundamentals_data"]
    _get_latest_value_with_type = funcs["_get_latest_value_with_type"]

    codes = [c["code"] for c in candidates]
    candidate_map = {c["code"]: c for c in candidates}

    # lastUpdated_irbank が古い順にソート（キャッシュにない = 最優先）
    def irbank_age(code):
        item = cache.get(code, {})
        return item.get("lastUpdated_irbank", item.get("lastUpdated", 0))

    codes_sorted = sorted(codes, key=irbank_age)

    if limit > 0:
        target_codes = codes_sorted[:limit]
    else:
        target_codes = codes_sorted[:batch_size]

    # 対象の最古データ日時を表示
    if target_codes:
        oldest_ts = irbank_age(target_codes[0])
        if oldest_ts > 0:
            oldest_date = time.strftime('%Y-%m-%d', time.localtime(oldest_ts))
            print(f"IR BANK更新: {len(target_codes)}銘柄（最古: {oldest_date}）")
        else:
            uncached = sum(1 for c in target_codes if c not in cache)
            print(f"IR BANK更新: {len(target_codes)}銘柄（未キャッシュ: {uncached}）")

    updated = 0
    errors = 0
    consecutive_302 = 0  # 連続302エラーカウント
    start_time = time.time()

    for i, code in enumerate(target_codes):
        progress = f"[{i+1}/{len(target_codes)}]"
        cand = candidate_map.get(code, {})
        result = {
            "code": code,
            "name": cand.get("name", ""),
            "sector": cand.get("sector", ""),
        }

        # Yahoo Financeから株価
        try:
            symbol = f"{code}.T"
            div_data = fetch_dividend_data(symbol)
            result["price"] = div_data.get("price", 0)
        except Exception as e:
            print(f"  {progress} {code}: 株価取得エラー ({e})")

        # IR BANKアクセス間隔
        time.sleep(interval)

        # 50銘柄ごとに5分休憩（過剰アクセス防止）
        if i > 0 and i % 50 == 0:
            save_cache(cache)
            print(f"\n  --- {i}銘柄処理済み。5分間休憩中... ---\n")
            time.sleep(300)

        # IR BANKからファンダメンタルズ
        try:
            funda = fetch_fundamentals_data(code)

            if funda.get("error"):
                error_msg = funda['error']
                print(f"  {progress} {code}: IR BANKエラー: {error_msg}")
                errors += 1
                # 302エラー連続検知 → 長めの休憩
                if "302" in str(error_msg):
                    consecutive_302 += 1
                    if consecutive_302 >= 5:
                        save_cache(cache)
                        wait_min = 10
                        print(f"\n  ⚠ 302エラーが{consecutive_302}回連続。{wait_min}分間休憩します...\n")
                        time.sleep(wait_min * 60)
                        consecutive_302 = 0
                else:
                    consecutive_302 = 0
                continue

            consecutive_302 = 0  # 成功したらリセット
            sections = funda.get("sections", {})
            periods = funda.get("periods", [])

            if not sections:
                print(f"  {progress} {code}: データ空（アクセス制限の可能性）")
                errors += 1
                continue

            # 配当
            dividend_val, dividend_type = _get_latest_value_with_type(
                sections.get("dividend", {})
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
                sections.get("eps", {})
            )
            result["eps"] = eps_val
            result["epsType"] = eps_type

            # BPS
            bps_val, bps_type = _get_latest_value_with_type(
                sections.get("bps", {})
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

            # その他（値だけ取得、型は不要）
            om_val, _ = _get_latest_value_with_type(sections.get("operatingMargin", {}))
            result["operatingMargin"] = om_val
            roe_val, _ = _get_latest_value_with_type(sections.get("roe", {}))
            result["roe"] = roe_val
            eq_val, _ = _get_latest_value_with_type(sections.get("equityRatio", {}))
            result["equityRatio"] = eq_val
            po_val, _ = _get_latest_value_with_type(sections.get("payoutRatio", {}))
            result["payoutRatio"] = po_val

            now = int(time.time())
            result["lastUpdated"] = now
            result["lastUpdated_irbank"] = now
            result["lastUpdated_yahoo"] = now
            cache[code] = result
            updated += 1
            print(f"  {progress} {code}: OK 配当={dividend_val}({dividend_type}) 利回り={result.get('dividendYield')}%")

        except Exception as e:
            print(f"  {progress} {code}: ファンダメンタルズ取得エラー ({e})")
            errors += 1

        # 5銘柄ごとに途中保存
        if (i + 1) % 5 == 0:
            save_cache(cache)

    save_cache(cache)
    elapsed = time.time() - start_time
    print(f"\nIR BANK更新完了: {updated}銘柄更新, {errors}エラー ({elapsed:.0f}秒 = {elapsed/60:.1f}分)")
    return cache


def main():
    print("=" * 60)
    print(f"スクリーニングデータ バッチ更新")
    print(f"開始時刻: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    parser = argparse.ArgumentParser(description="スクリーニングデータの事前バッチ取得")
    parser.add_argument("--mode", type=str, default="full",
                        choices=["yahoo", "irbank", "full"],
                        help="更新モード: yahoo(株価のみ), irbank(決算データ), full(両方)")
    parser.add_argument("--market", type=str, default="", help="市場区分フィルター（例: プライム）")
    parser.add_argument("--limit", type=int, default=0, help="取得銘柄数の上限（0=デフォルト）")
    parser.add_argument("--batch-size", type=int, default=500, help="IR BANKモードの1回あたり取得数")
    parser.add_argument("--interval", type=float, default=3.0, help="IR BANKアクセス間隔（秒）")
    args = parser.parse_args()

    candidates = load_candidates(args.market)
    cache = load_cache()
    print(f"候補銘柄: {len(candidates)}銘柄")
    print(f"既存キャッシュ: {len(cache)}銘柄")
    print(f"モード: {args.mode}")
    print()

    if args.mode == "yahoo":
        cache = mode_yahoo(candidates, cache, limit=args.limit)
    elif args.mode == "irbank":
        cache = mode_irbank(candidates, cache,
                            batch_size=args.batch_size,
                            interval=args.interval,
                            limit=args.limit)
    elif args.mode == "full":
        print("--- Phase 1: IR BANK ---")
        cache = mode_irbank(candidates, cache,
                            batch_size=args.batch_size if args.limit == 0 else args.limit,
                            interval=args.interval,
                            limit=args.limit)
        print()
        print("--- Phase 2: Yahoo Finance ---")
        cache = mode_yahoo(candidates, cache, limit=args.limit)

    print()
    print("=" * 60)
    print(f"最終キャッシュ: {len(cache)}銘柄")
    print(f"終了時刻: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Render環境ではキャッシュをGitにコミット＆プッシュ
    if os.environ.get("RENDER"):
        git_commit_cache()


def git_commit_cache():
    """キャッシュファイルをGitにコミットしてプッシュ（Render環境用）"""
    import subprocess as sp
    try:
        print("\n[Git] キャッシュをGitにコミット中...")
        sp.run(["git", "add", "screening-cache.json", "fundamentals-cache.json"],
               cwd=_base_dir, timeout=10, capture_output=True)
        result = sp.run(
            ["git", "commit", "-m", f"[AUTO] Cache update {time.strftime('%Y-%m-%d %H:%M')}"],
            cwd=_base_dir, timeout=10, capture_output=True, text=True
        )
        if "nothing to commit" in result.stdout:
            print("[Git] 変更なし（コミットスキップ）")
            return
        sp.run(["git", "push"], cwd=_base_dir, timeout=60, capture_output=True)
        print("[Git] キャッシュをプッシュしました")
    except Exception as e:
        print(f"[Git] コミット/プッシュエラー（無視可能）: {e}")


if __name__ == "__main__":
    main()
