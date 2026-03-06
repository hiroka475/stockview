# スクリーニングデータ取得のリファクタリング計画

作成日: 2026-03-06

## 背景

- IR BANKへの過剰アクセスで表示制限を受けた
- Yahoo Finance v10 APIが401 Unauthorized（認証必須化）
- 配当データがYahoo FinanceとIR BANKで食い違うケースがある（例: ナカボーテック 300円 vs 260円）
- Yahoo Financeは日本株の会社予想更新が遅い

## 方針: データソースの一本化

- **株価のみ Yahoo Finance**（リアルタイム性が必要なため）
- **それ以外は全て IR BANK**（配当、EPS、BPS、営業利益率、ROE、自己資本比率、配当性向）
- PER/PBR は IR BANK の EPS/BPS と Yahoo の株価から計算
  - PER = 株価 ÷ EPS
  - PBR = 株価 ÷ BPS

## EPS/BPS の優先順位

- **予想値を優先、なければ実績値を使う**
- IR BANKの`fy-data-all.json`に予想値が含まれているか要確認（アクセス制限解除後）
- 予想値がなければ、IR BANKの決算まとめページから取得する方法も検討

## 配当利回りの定義

- StockView: 年間配当（予想優先）÷ 現在株価 × 100
- TradingView（参考）: 過去12ヶ月実績配当 ÷ 現在株価 × 100（TTM方式）
- StockViewの方式の方が高配当株分析には実用的（増配/減配がすぐ反映される）

## 済み: キャッシュ強化（2026-03-06実施済み）

server.py に以下の修正を適用済み:

1. ファンダメンタルズ専用キャッシュ新設（7日間TTL + ファイル永続化）
   - `_fundamentals_cache` / `_FUNDAMENTALS_CACHE_TTL = 604800`
   - `fundamentals-cache.json` に永続化
2. IR BANKアクセス間隔制御（最低3秒間隔）
   - `_wait_irbank_rate_limit()` 関数追加
3. スクリーニングバッチ間待機を 0.5秒 → 3秒に拡大

## 済み: データソース一本化（2026-03-07実施済み）

### `_fetch_screening_data_static()` を全面改修

変更内容:
- **株価のみ Yahoo Finance** (`fetch_dividend_data()` から `price` だけ使用)
- **配当**: IR BANK の `dividend` セクション（予想優先、`_get_latest_value_with_type()` 使用）
- **配当利回り**: IR BANK配当 ÷ Yahoo株価 × 100 で自前計算
- **EPS/BPS**: IR BANK（予想優先、`_get_latest_value_with_type()` 使用）
- **PER**: Yahoo株価 ÷ IR BANK EPS で計算（予想EPS優先）
- **PBR**: Yahoo株価 ÷ IR BANK BPS で計算（予想BPS優先）
- **営業利益率/ROE/自己資本比率/配当性向**: IR BANK（変更なし）
- `_fetch_yahoo_valuation_safe()` はスクリーニングから呼ばなくなった

### 新規ヘルパー関数 `_get_latest_value_with_type()`

- 直近の非null値を返すと同時に「予想」or「実績」を判定
- 判定基準: 期末月+3ヶ月以内なら「予想」（決算発表は期末から2-3ヶ月後のため）

## 済み: バッチ取得 + キャッシュ優先モード（2026-03-07実施済み）

### 問題: 共有時のIR BANKアクセス制限

複数ユーザーがスクリーニングを実行するとIR BANKへのアクセスが集中し制限がかかる。

### 解決策: 事前バッチ取得 + キャッシュ優先

1. **`update-screening-data.py`（新規作成）**
   - 毎日1回実行して全候補銘柄のデータをIR BANK + Yahoo Financeから取得
   - screening-cache.json に保存
   - 12時間以内のキャッシュがある銘柄は株価のみ更新（IR BANKアクセスなし）
   - 5銘柄ごとに途中保存（中断しても失われない）
   - エラー時は既存キャッシュを維持

2. **server.py の変更**
   - スクリーニングキャッシュTTLを24時間 → 7日間に延長
   - バッチAPIに `force` パラメータ追加（true=IR BANKから再取得、false=キャッシュ優先）

3. **screening.html の変更**
   - 「データ表示」ボタン: キャッシュから即座に表示（IR BANKアクセスなし）
   - 「強制再取得」ボタン: 確認ダイアログ付きでIR BANKから再取得
   - ページ読み込み時にキャッシュデータを自動表示
   - データ更新日時を表示

4. **定期実行（crontab）**
   ```
   0 5 * * * cd /Users/ogurahirokazu/claude-code-lab/stock-view && python3 update-screening-data.py >> update-screening.log 2>&1
   ```

## 検証用スクリプト

- `verify-screening.py` を作成済み（yfinance使用）
- リファクタリング後に再度実行して正確性を確認する
