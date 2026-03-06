"""
StockView - 株価データAPIサーバー
Yahoo Financeから株価データを取得し、フロントエンドに提供する
"""

import gzip
import io
import json
import os
import ssl
import subprocess
import sys
import urllib.request
import urllib.parse
import urllib.error
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import HTTPServer, SimpleHTTPRequestHandler
from datetime import datetime, timedelta

# brotli モジュール（IR BANKのレスポンス解凍に必要）
# なければ自動インストールを試みる
try:
    import brotli
    _HAS_BROTLI = True
except ImportError:
    print("  brotliモジュールが見つかりません。インストールします...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "brotli", "-q"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        import brotli
        _HAS_BROTLI = True
        print("  brotliモジュールのインストール完了")
    except Exception as e:
        _HAS_BROTLI = False
        print(f"  brotliモジュールのインストール失敗: {e}")
        print("  ファンダメンタルズ機能が制限されます。手動で pip install brotli を実行してください。")

# macOSでPythonのSSL証明書が見つからない問題を回避
# Yahoo Finance APIへのHTTPS接続に必要
ssl_context = ssl.create_default_context()
try:
    import certifi
    ssl_context.load_verify_locations(certifi.where())
except ImportError:
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

# 銘柄マスタを読み込む（サーバー起動時に1回だけ）
_base_dir = os.path.dirname(os.path.abspath(__file__))

JP_STOCKS = []
_jp_stocks_path = os.path.join(_base_dir, "jp-stocks.json")
if os.path.exists(_jp_stocks_path):
    with open(_jp_stocks_path, "r", encoding="utf-8") as f:
        JP_STOCKS = json.load(f)
    print(f"  日本株マスタ: {len(JP_STOCKS)}銘柄 読み込み完了")

US_STOCKS = []
_us_stocks_path = os.path.join(_base_dir, "us-stocks.json")
if os.path.exists(_us_stocks_path):
    with open(_us_stocks_path, "r", encoding="utf-8") as f:
        US_STOCKS = json.load(f)
    print(f"  米国株マスタ: {len(US_STOCKS)}銘柄 読み込み完了")

# スクリーニング候補リスト
SCREENING_CANDIDATES = []
_candidates_path = os.path.join(_base_dir, "dividend-candidates.json")
if os.path.exists(_candidates_path):
    with open(_candidates_path, "r", encoding="utf-8") as f:
        SCREENING_CANDIDATES = json.load(f)
    print(f"  スクリーニング候補: {len(SCREENING_CANDIDATES)}銘柄 読み込み完了")

# スクリーニング用キャッシュ（長期保持: 30日間）
# IR BANKのデータは決算発表時（年4回）しか変わらないため30日キャッシュで十分。
# 株価のみサーバー起動時に毎回Yahoo Financeから更新する。
_screening_cache = {}
_SCREENING_CACHE_TTL = 2592000  # 30日間（秒）
_SCREENING_CACHE_FILE = os.path.join(_base_dir, "screening-cache.json")

# ファンダメンタルズ専用キャッシュ（長期保持: 30日間 + ファイル永続化）
# IR BANKのデータは決算発表時（年4回）しか変わらないため30日キャッシュで十分。
_fundamentals_cache = {}
_FUNDAMENTALS_CACHE_TTL = 2592000  # 30日間（秒）
_FUNDAMENTALS_CACHE_FILE = os.path.join(_base_dir, "fundamentals-cache.json")

# IR BANKへのアクセス間隔制御（過剰アクセス防止）
_last_irbank_access = 0
_IRBANK_ACCESS_INTERVAL = 3  # 最低3秒間隔

# 起動時にキャッシュファイルがあれば読み込む
if os.path.exists(_SCREENING_CACHE_FILE):
    try:
        with open(_SCREENING_CACHE_FILE, "r", encoding="utf-8") as f:
            _saved = json.load(f)
        for code, data in _saved.items():
            _screening_cache[f"screening|{code}"] = {
                "data": data,
                "timestamp": data.get("lastUpdated", time.time()),
            }
        print(f"  スクリーニングキャッシュ: {len(_saved)}銘柄 読み込み完了")
    except Exception as e:
        print(f"  スクリーニングキャッシュ読み込みエラー: {e}")

# 起動時にファンダメンタルズキャッシュファイルがあれば読み込む
if os.path.exists(_FUNDAMENTALS_CACHE_FILE):
    try:
        with open(_FUNDAMENTALS_CACHE_FILE, "r", encoding="utf-8") as f:
            _saved_funda = json.load(f)
        for code, entry in _saved_funda.items():
            _fundamentals_cache[f"fundamentals|{code}"] = {
                "data": entry.get("data", entry),
                "timestamp": entry.get("timestamp", time.time()),
            }
        print(f"  ファンダメンタルズキャッシュ: {len(_saved_funda)}銘柄 読み込み完了")
    except Exception as e:
        print(f"  ファンダメンタルズキャッシュ読み込みエラー: {e}")


# ==========================================
# サーバー側メモリキャッシュ
# Yahoo Finance APIへのリクエスト結果をキャッシュし、
# 同じ銘柄・パラメータの2回目以降のリクエストを高速化する
# ==========================================
_api_cache = {}
_API_CACHE_TTL = 180  # 3分間有効（秒）
_API_CACHE_MAX = 200  # 最大200エントリ


def get_api_cache(key):
    """キャッシュからデータを取得（TTL切れなら None）"""
    entry = _api_cache.get(key)
    if entry is None:
        return None
    if time.time() - entry["timestamp"] > _API_CACHE_TTL:
        del _api_cache[key]
        return None
    return entry["data"]


def set_api_cache(key, data):
    """キャッシュにデータを保存"""
    # 容量超過時は最も古いものを削除
    if len(_api_cache) >= _API_CACHE_MAX:
        oldest_key = min(_api_cache, key=lambda k: _api_cache[k]["timestamp"])
        del _api_cache[oldest_key]
    _api_cache[key] = {"data": data, "timestamp": time.time()}


class StockAPIHandler(SimpleHTTPRequestHandler):
    """株価データAPIとフロントエンドの静的ファイルを配信するHTTPハンドラ"""

    def do_GET(self):
        """GETリクエストの処理"""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # APIルート
        if path.startswith("/api/fundamentals/"):
            self.handle_fundamentals_api(path, query)
        elif path.startswith("/api/dividend/"):
            self.handle_dividend_api(path, query)
        elif path.startswith("/api/stock/"):
            self.handle_stock_api(path, query)
        elif path.startswith("/api/search"):
            self.handle_search_api(query)
        elif path == "/api/alerts/load":
            self.handle_alerts_load()
        elif path == "/api/screening/candidates":
            self.handle_screening_candidates()
        else:
            # 静的ファイル（HTML/CSS/JS）を配信
            super().do_GET()

    def end_headers(self):
        """HTMLファイルのキャッシュを無効にする"""
        if hasattr(self, 'path') and (self.path.endswith('.html') or self.path == '/'):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def do_POST(self):
        """POSTリクエストの処理"""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/alerts/save":
            self.handle_alerts_save()
        elif path == "/api/alerts/check":
            self.handle_alerts_check()
        elif path == "/api/screening/batch":
            self.handle_screening_batch()
        else:
            self.send_json_error("Not Found", 404)


    # ==========================================
    # アラート関連API
    # ==========================================

    def _get_alerts_path(self):
        """alerts.jsonのパスを返す"""
        return os.path.join(_base_dir, "alerts.json")

    def _read_body_json(self):
        """POSTリクエストのJSONボディを読み取る"""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        return json.loads(body.decode("utf-8"))

    def handle_alerts_load(self):
        """GET /api/alerts/load — 保存されたアラート設定を読み込む"""
        alerts_path = self._get_alerts_path()
        if os.path.exists(alerts_path):
            with open(alerts_path, "r", encoding="utf-8") as f:
                alerts = json.load(f)
        else:
            alerts = {}
        self.send_json_response(alerts)

    def handle_alerts_save(self):
        """POST /api/alerts/save — アラート設定をalerts.jsonに保存"""
        try:
            alerts = self._read_body_json()
            alerts_path = self._get_alerts_path()
            with open(alerts_path, "w", encoding="utf-8") as f:
                json.dump(alerts, f, ensure_ascii=False, indent=2)
            self.send_json_response({"status": "ok"})
        except Exception as e:
            self.send_json_error(f"アラート保存エラー: {e}", 500)

    def handle_alerts_check(self):
        """
        POST /api/alerts/check — 全アラートの価格チェック
        現在の株価を取得し、アラート条件に該当する銘柄リストを返す
        """
        try:
            alerts_path = self._get_alerts_path()
            if not os.path.exists(alerts_path):
                self.send_json_response({"triggered": []})
                return

            with open(alerts_path, "r", encoding="utf-8") as f:
                alerts = json.load(f)

            triggered = []
            for symbol, alert_info in alerts.items():
                if not alert_info.get("enabled", False):
                    continue

                alert_price = alert_info.get("price", 0)
                direction = alert_info.get("direction", "below")

                # Yahoo Financeから現在の株価を取得
                try:
                    yahoo_symbol = symbol
                    code = symbol.replace(".T", "")
                    if code.isdigit() and len(code) == 4:
                        yahoo_symbol = f"{code}.T"

                    data = fetch_yahoo_finance(yahoo_symbol, "1d", "5d")
                    closes = data.get("closes", [])
                    if not closes:
                        continue

                    # 最新の有効な終値を取得
                    current_price = None
                    for c in reversed(closes):
                        if c is not None:
                            current_price = c
                            break

                    if current_price is None:
                        continue

                    # アラート条件チェック
                    is_triggered = False
                    if direction == "below" and current_price <= alert_price:
                        is_triggered = True

                    if is_triggered:
                        triggered.append({
                            "symbol": symbol,
                            "name": alert_info.get("name", code),
                            "alertPrice": alert_price,
                            "currentPrice": current_price,
                            "direction": direction,
                        })
                except Exception as e:
                    print(f"  [AlertCheck] {symbol} の価格取得エラー: {e}")
                    continue

            self.send_json_response({"triggered": triggered})
        except Exception as e:
            self.send_json_error(f"アラートチェックエラー: {e}", 500)

    # ==========================================
    # スクリーニング関連API
    # ==========================================

    def handle_screening_candidates(self):
        """GET /api/screening/candidates — 候補銘柄リストを返す"""
        self.send_json_response(SCREENING_CANDIDATES)

    def handle_screening_batch(self):
        """
        POST /api/screening/batch — 複数銘柄のスクリーニングデータを一括取得（並列処理）
        リクエスト: {"codes": ["7203", "8306", ...], "force": false}
        レスポンス: {"results": {code: {...metrics...}, ...}, "errors": [...], "cached": N, "fetched": N}

        force=true の場合はキャッシュを無視してIR BANKから再取得する。
        force=false（デフォルト）の場合はキャッシュ優先で返す。
        """
        try:
            body = self._read_body_json()
            codes = body.get("codes", [])
            force_refresh = body.get("force", False)
            if not codes:
                self.send_json_response({"results": {}, "errors": [], "cached": 0, "fetched": 0})
                return

            results = {}
            errors = []
            codes_to_fetch = []

            # キャッシュヒットチェック
            for code in codes:
                if force_refresh:
                    # 強制再取得モード: 全銘柄を再取得
                    codes_to_fetch.append(code)
                    continue

                cache_key = f"screening|{code}"
                cached = _screening_cache.get(cache_key)
                if cached and (time.time() - cached["timestamp"] < _SCREENING_CACHE_TTL):
                    d = cached["data"]
                    has_irbank_data = d.get("dividendType") is not None  # 新形式チェック
                    has_valuation = (d.get("per") not in (None, 0)) or (d.get("pbr") not in (None, 0))
                    has_dividend = (d.get("dividendYield") not in (None, 0))
                    if has_irbank_data and (has_valuation or not has_dividend):
                        results[code] = d
                    else:
                        codes_to_fetch.append(code)
                else:
                    codes_to_fetch.append(code)

            cached_count = len(results)

            # キャッシュミスした銘柄を並列取得（最大5並列、レート制限対策）
            if codes_to_fetch:
                # 小バッチに分けてウェイトを入れる
                mini_batch_size = 5
                for mb_start in range(0, len(codes_to_fetch), mini_batch_size):
                    mb = codes_to_fetch[mb_start:mb_start + mini_batch_size]
                    with ThreadPoolExecutor(max_workers=5) as executor:
                        future_map = {
                            executor.submit(_fetch_screening_data_static, code): code
                            for code in mb
                        }
                        for future in as_completed(future_map):
                            code = future_map[future]
                            try:
                                data = future.result()
                                if data:
                                    results[code] = data
                                    # IR BANKエラーのデータはキャッシュしない（次回再取得させる）
                                    if not data.get("_irbank_error"):
                                        _screening_cache[f"screening|{code}"] = {
                                            "data": data,
                                            "timestamp": time.time(),
                                        }
                                    else:
                                        errors.append({"code": code, "error": data["_irbank_error"]})
                            except Exception as e:
                                errors.append({"code": code, "error": str(e)})
                    # バッチ間に待機（IR BANKレート制限対策: 3秒間隔）
                    if mb_start + mini_batch_size < len(codes_to_fetch):
                        time.sleep(3)

                # 取得後にキャッシュファイルに保存
                _save_screening_cache()

            self.send_json_response({
                "results": results,
                "errors": errors,
                "cached": cached_count,
                "fetched": len(codes_to_fetch),
            })
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.send_json_error(f"バッチ取得エラー: {e}", 500)

    def handle_stock_api(self, path, query):
        """
        株価データAPIの処理
        URL: /api/stock/{symbol}?interval=1d&range=1y
        """
        # URLからシンボルを取り出す（例: /api/stock/AAPL → AAPL）
        symbol = path.replace("/api/stock/", "").strip("/")
        if not symbol:
            self.send_json_error("銘柄コードが指定されていません", 400)
            return

        # クエリパラメータの取得（デフォルト値付き）
        interval = query.get("interval", ["1d"])[0]  # 1d, 1wk, 1mo
        date_range = query.get("range", ["1y"])[0]  # 1mo, 3mo, 6mo, 1y, 5y, 10y, 15y, max

        # 日本株の場合は末尾に ".T" を追加（東証の識別子）
        yahoo_symbol = symbol
        if symbol.isdigit() and len(symbol) == 4:
            yahoo_symbol = f"{symbol}.T"

        try:
            # サーバー側キャッシュを確認
            cache_key = f"stock|{yahoo_symbol}|{interval}|{date_range}"
            cached = get_api_cache(cache_key)
            if cached:
                self.send_json_response(cached)
                return

            data = fetch_yahoo_finance(yahoo_symbol, interval, date_range)
            set_api_cache(cache_key, data)
            self.send_json_response(data)
        except Exception as e:
            self.send_json_error(f"データ取得エラー: {str(e)}", 500)

    def handle_dividend_api(self, path, query):
        """
        配当データAPIの処理
        URL: /api/dividend/{symbol}
        Yahoo Finance の quoteSummary API から配当情報を取得する
        - Forward（予想）配当を優先、なければTrailing（実績）にフォールバック
        """
        symbol = path.replace("/api/dividend/", "").strip("/")
        if not symbol:
            self.send_json_error("銘柄コードが指定されていません", 400)
            return

        # 日本株の場合は末尾に ".T" を追加
        yahoo_symbol = symbol
        if symbol.replace(".T", "").isdigit() and len(symbol.replace(".T", "")) == 4:
            if not symbol.endswith(".T"):
                yahoo_symbol = f"{symbol}.T"

        try:
            # サーバー側キャッシュを確認
            cache_key = f"dividend|{yahoo_symbol}"
            cached = get_api_cache(cache_key)
            if cached:
                self.send_json_response(cached)
                return

            data = fetch_dividend_data(yahoo_symbol)
            set_api_cache(cache_key, data)
            self.send_json_response(data)
        except Exception as e:
            self.send_json_error(f"配当データ取得エラー: {str(e)}", 500)

    def handle_fundamentals_api(self, path, query):
        """
        ファンダメンタルズデータAPIの処理
        URL: /api/fundamentals/{code}
        IR BANKからファンダメンタルズデータを取得する（日本株のみ対応）
        """
        code = path.replace("/api/fundamentals/", "").strip("/")
        # .Tを除去して4桁コードにする
        code = code.replace(".T", "")
        if not code or not code.isdigit() or len(code) != 4:
            self.send_json_error("日本株の4桁コードを指定してください", 400)
            return

        try:
            # ファンダメンタルズ専用キャッシュを確認（7日間TTL）
            cache_key = f"fundamentals|{code}"
            cached_entry = _fundamentals_cache.get(cache_key)
            if cached_entry and (time.time() - cached_entry["timestamp"] < _FUNDAMENTALS_CACHE_TTL):
                print(f"  IR BANK ({code}): キャッシュヒット（残り{int((_FUNDAMENTALS_CACHE_TTL - (time.time() - cached_entry['timestamp'])) / 3600)}時間）")
                self.send_json_response(cached_entry["data"])
                return

            data = fetch_fundamentals_data(code)
            # エラーがなければキャッシュ（メモリ+ファイル）
            if not data.get("error"):
                _fundamentals_cache[cache_key] = {"data": data, "timestamp": time.time()}
                _save_fundamentals_cache()
            self.send_json_response(data)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.send_json_error(f"ファンダメンタルズデータ取得エラー: {str(e)}", 500)

    def handle_search_api(self, query):
        """
        銘柄検索APIの処理
        URL: /api/search?q=apple&market=US
        """
        search_query = query.get("q", [""])[0]
        market = query.get("market", ["US"])[0]

        if not search_query:
            self.send_json_response({"results": []})
            return

        try:
            results = search_stocks(search_query, market)
            self.send_json_response({"results": results})
        except Exception as e:
            self.send_json_error(f"検索エラー: {str(e)}", 500)

    def send_json_response(self, data, status=200):
        """JSON形式でレスポンスを返す"""
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def send_json_error(self, message, status=500):
        """エラーをJSON形式で返す"""
        self.send_json_response({"error": message}, status)

    def log_message(self, format, *args):
        """ログメッセージを日本語フレンドリーにする"""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]}")


# ==========================================
# スクリーニング用ユーティリティ関数
# ==========================================

def _get_latest_value(section_data):
    """セクションデータの直近の有効な値を取得する"""
    values = section_data.get("values", [])
    for v in reversed(values):
        if v is not None:
            return v
    return None


def _get_latest_value_with_type(section_data, periods):
    """
    セクションデータの直近の有効な値と、予想/実績の種別を返す。
    IR BANKのperiod（例: "2025/03"）が現在日付より未来なら「予想」、過去なら「実績」。

    Returns:
        (value, type_str): 値と種別（"予想" or "実績"）のタプル
    """
    values = section_data.get("values", [])
    now = datetime.now()
    for i in range(len(values) - 1, -1, -1):
        if values[i] is not None:
            # 期間の末日と現在日付を比較して予想/実績を判定
            val_type = "実績"
            if i < len(periods):
                period_str = periods[i]
                try:
                    parts = period_str.split("/")
                    year = int(parts[0])
                    month = int(parts[1]) if len(parts) > 1 else 12
                    # 期末月の翌月末までに決算発表されるため、期末+3ヶ月を目安に判定
                    # 例: 2025/03期 → 2025年6月頃までは予想扱い
                    from datetime import date
                    fiscal_end = date(year, month, 1)
                    # 決算発表は通常期末から2-3ヶ月後なので、期末+3ヶ月以内なら予想
                    months_since_end = (now.year - fiscal_end.year) * 12 + (now.month - fiscal_end.month)
                    if months_since_end < 3:
                        val_type = "予想"
                except (ValueError, IndexError):
                    pass
            return values[i], val_type
    return None, "不明"


def _calc_growth_rate(section_data):
    """セクションデータから前年比成長率を計算する"""
    values = section_data.get("values", [])
    # 直近2つの有効な値を取得
    recent = []
    for v in reversed(values):
        if v is not None and v != 0:
            recent.append(v)
            if len(recent) == 2:
                break
    if len(recent) < 2:
        return None
    current, previous = recent[0], recent[1]
    return round((current - previous) / abs(previous) * 100, 1)


def _calc_consecutive_dividend_years(section_data):
    """連続増配年数を計算する"""
    values = section_data.get("values", [])
    # 末尾からNoneを除去
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return 0
    years = 0
    for i in range(len(clean) - 1, 0, -1):
        if clean[i] >= clean[i - 1] and clean[i - 1] > 0:
            years += 1
        else:
            break
    return years


def _fetch_yahoo_valuation_safe(code):
    """
    Yahoo Finance v10 APIからPER/PBRを取得する（エラー時は空dictを返す）
    """
    symbol = f"{code}.T"
    url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}?modules=summaryDetail,defaultKeyStatistics"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        })
        with urllib.request.urlopen(req, timeout=8, context=ssl_context) as response:
            data = json.loads(response.read().decode("utf-8"))

        result_data = data.get("quoteSummary", {}).get("result", [])
        if not result_data:
            return {}

        info = result_data[0]
        summary = info.get("summaryDetail", {})
        key_stats = info.get("defaultKeyStatistics", {})

        def get_raw(obj, key):
            val = obj.get(key, {})
            if isinstance(val, dict):
                return val.get("raw")
            return val

        metrics = {}

        # 予想PER（Forward P/E）
        fpe = get_raw(summary, "forwardPE") or get_raw(key_stats, "forwardPE")
        if fpe is not None and fpe > 0:
            metrics["forwardPER"] = round(fpe, 2)

        # 実績PER（Trailing P/E）
        tpe = get_raw(summary, "trailingPE") or get_raw(key_stats, "trailingPE")
        if tpe is not None and tpe > 0:
            metrics["trailingPER"] = round(tpe, 2)

        # PBR（Price to Book）
        pb = get_raw(key_stats, "priceToBook") or get_raw(summary, "priceToBook")
        if pb is not None and pb > 0:
            metrics["pbr"] = round(pb, 2)

        if metrics:
            print(f"  Yahoo Finance v10 ({code}): バリュエーション取得成功 = {metrics}")
        return metrics
    except Exception as e:
        print(f"  Yahoo Finance v10 ({code}): 取得失敗 ({e}) → IR BANKフォールバック")
        return {}


def _fetch_screening_data_static(code):
    """
    1銘柄のスクリーニング用データを取得する（スレッドから呼べるスタティック関数）
    株価のみYahoo Finance、それ以外は全てIR BANKから取得する。
    EPS/BPS/配当は予想値を優先し、なければ実績値を使用する。
    PER/PBR = Yahoo株価 ÷ IR BANK EPS/BPS で計算。
    """
    result = {
        "code": code,
        "name": "",
        "sector": "",
    }

    # マスタデータから名前・セクターを取得
    for s in SCREENING_CANDIDATES:
        if s["code"] == code:
            result["name"] = s["name"]
            result["sector"] = s["sector"]
            break

    # ===== Yahoo Finance から株価のみ取得（リトライ付き）=====
    symbol = f"{code}.T"
    for attempt in range(3):
        try:
            div_data = fetch_dividend_data(symbol)
            result["price"] = div_data.get("price", 0)
            print(f"  [Screening] {code} 株価: Yahoo Finance = {result['price']}")
            break
        except Exception as e:
            if attempt < 2 and ("401" in str(e) or "429" in str(e) or "500" in str(e)):
                time.sleep(1 + attempt)
                continue
            print(f"  [Screening] {code} 株価取得エラー: {e}")

    # ===== IR BANK からファンダメンタルズ全て取得（リトライ付き）=====
    irbank_ok = False
    for attempt in range(3):
        try:
            funda = fetch_fundamentals_data(code)

            # IR BANKのエラーチェック（アクセス制限等）
            if funda.get("error"):
                print(f"  [Screening] {code} IR BANKエラー: {funda['error']}")
                if attempt < 2:
                    time.sleep(2 + attempt)
                    continue
                result["_irbank_error"] = funda["error"]
                break

            sections = funda.get("sections", {})
            periods = funda.get("periods", [])

            # データが空でないことを確認（アクセス制限時は空のsectionsが返る）
            if not sections or all(
                all(v is None for v in sec.get("values", []))
                for sec in sections.values()
            ):
                print(f"  [Screening] {code} IR BANKデータ空（アクセス制限の可能性）")
                if attempt < 2:
                    time.sleep(2 + attempt)
                    continue
                result["_irbank_error"] = "IR BANKからデータを取得できませんでした（アクセス制限の可能性）"
                break

            # --- 配当（IR BANK、予想優先）---
            dividend_val, dividend_type = _get_latest_value_with_type(
                sections.get("dividend", {}), periods
            )
            result["dividendAnnual"] = dividend_val
            result["dividendType"] = dividend_type
            print(f"  [Screening] {code} 配当: IR BANK {dividend_type} = {dividend_val}")

            # --- 配当利回り（IR BANK配当 ÷ Yahoo株価）---
            price = result.get("price", 0)
            if price and price > 0 and dividend_val and dividend_val > 0:
                result["dividendYield"] = round(dividend_val / price * 100, 2)
                print(f"  [Screening] {code} 配当利回り: {dividend_val}÷{price}×100 = {result['dividendYield']}%")
            else:
                result["dividendYield"] = 0

            # --- EPS（IR BANK、予想優先）---
            eps_val, eps_type = _get_latest_value_with_type(
                sections.get("eps", {}), periods
            )
            result["eps"] = eps_val
            result["epsType"] = eps_type
            print(f"  [Screening] {code} EPS: IR BANK {eps_type} = {eps_val}")

            # --- BPS（IR BANK、予想優先）---
            bps_val, bps_type = _get_latest_value_with_type(
                sections.get("bps", {}), periods
            )
            result["bps"] = bps_val
            result["bpsType"] = bps_type

            # --- PER = 株価 ÷ EPS（予想優先）---
            if price and price > 0 and eps_val and eps_val > 0:
                result["per"] = round(price / eps_val, 2)
                result["perType"] = eps_type
                print(f"  [Screening] {code} PER: {price}÷{eps_val} = {result['per']} ({eps_type})")

            # --- PBR = 株価 ÷ BPS（予想優先）---
            if price and price > 0 and bps_val and bps_val > 0:
                result["pbr"] = round(price / bps_val, 2)
                result["pbrType"] = bps_type
                print(f"  [Screening] {code} PBR: {price}÷{bps_val} = {result['pbr']} ({bps_type})")

            # --- 営業利益率・ROE・自己資本比率・配当性向（IR BANK）---
            result["operatingMargin"] = _get_latest_value(sections.get("operatingMargin", {}))
            result["roe"] = _get_latest_value(sections.get("roe", {}))
            result["equityRatio"] = _get_latest_value(sections.get("equityRatio", {}))
            result["payoutRatio"] = _get_latest_value(sections.get("payoutRatio", {}))

            # --- 売上成長率・連続増配年数 ---
            result["revenueGrowth1y"] = _calc_growth_rate(sections.get("revenue", {}))
            result["consecutiveDividendYears"] = _calc_consecutive_dividend_years(sections.get("dividend", {}))

            irbank_ok = True
            break
        except Exception as e:
            if attempt < 2 and ("401" in str(e) or "429" in str(e) or "500" in str(e)):
                time.sleep(1 + attempt)
                continue
            print(f"  [Screening] {code} ファンダメンタルズ取得エラー: {e}")
            result["_irbank_error"] = str(e)

    result["lastUpdated"] = int(time.time())
    return result


def _save_screening_cache():
    """スクリーニングキャッシュをファイルに保存"""
    try:
        cache_data = {}
        for key, entry in _screening_cache.items():
            if key.startswith("screening|"):
                code = key.replace("screening|", "")
                cache_data[code] = entry["data"]
        with open(_SCREENING_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False)
        print(f"  [Cache] screening-cache.json 保存完了 ({len(cache_data)}銘柄)")
    except Exception as e:
        print(f"  [Cache] 保存エラー: {e}")


def _save_fundamentals_cache():
    """ファンダメンタルズキャッシュをファイルに保存"""
    try:
        cache_data = {}
        for key, entry in _fundamentals_cache.items():
            if key.startswith("fundamentals|"):
                code = key.replace("fundamentals|", "")
                cache_data[code] = {
                    "data": entry["data"],
                    "timestamp": entry["timestamp"],
                }
        with open(_FUNDAMENTALS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False)
        print(f"  [Cache] fundamentals-cache.json 保存完了 ({len(cache_data)}銘柄)")
    except Exception as e:
        print(f"  [Cache] ファンダメンタルズキャッシュ保存エラー: {e}")


def _wait_irbank_rate_limit():
    """IR BANKへのアクセス間隔を制御する"""
    global _last_irbank_access
    elapsed = time.time() - _last_irbank_access
    if elapsed < _IRBANK_ACCESS_INTERVAL:
        wait_time = _IRBANK_ACCESS_INTERVAL - elapsed
        print(f"  IR BANK: レート制限待機 {wait_time:.1f}秒...")
        time.sleep(wait_time)
    _last_irbank_access = time.time()


def fetch_yahoo_finance(symbol: str, interval: str, date_range: str) -> dict:
    """
    Yahoo Financeからチャートデータを取得する

    Args:
        symbol: 銘柄シンボル（例: "AAPL", "7203.T"）
        interval: 時間足（"1d", "1wk", "1mo"）
        date_range: 取得期間（"1mo", "3mo", "6mo", "1y", "5y"）

    Returns:
        dict: 整形された株価データ
    """
    # Yahoo Finance Chart API の URL を組み立てる
    base_url = "https://query1.finance.yahoo.com/v8/finance/chart/"

    # Yahoo Financeがサポートしないrange（3y, 15yなど）はperiod1/period2で対応
    unsupported_ranges = {
        "3y": 3 * 365,
        "15y": 15 * 365,
    }
    if date_range in unsupported_ranges:
        days = unsupported_ranges[date_range]
        period2 = int(time.time())
        period1 = period2 - (days * 86400)
        params = urllib.parse.urlencode({
            "interval": interval,
            "period1": period1,
            "period2": period2,
            "includePrePost": "false",
        })
    else:
        params = urllib.parse.urlencode({
            "interval": interval,
            "range": date_range,
            "includePrePost": "false",
        })
    url = f"{base_url}{urllib.parse.quote(symbol)}?{params}"

    # HTTPリクエストを送信
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    })

    with urllib.request.urlopen(req, timeout=10, context=ssl_context) as response:
        raw_data = json.loads(response.read().decode("utf-8"))

    # レスポンスの解析
    chart = raw_data["chart"]["result"][0]
    meta = chart["meta"]
    timestamps = chart["timestamp"]
    quotes = chart["indicators"]["quote"][0]

    # ローソク足データの整形
    candles = []
    for i in range(len(timestamps)):
        # Noneのデータ（休場日など）をスキップ
        if (quotes["open"][i] is None or quotes["close"][i] is None):
            continue

        candles.append({
            "date": datetime.utcfromtimestamp(timestamps[i]).strftime("%Y-%m-%d"),
            "time": timestamps[i],  # Lightweight Charts はUNIXタイムスタンプを使う
            "open": round(quotes["open"][i], 2),
            "high": round(quotes["high"][i], 2),
            "low": round(quotes["low"][i], 2),
            "close": round(quotes["close"][i], 2),
            "volume": int(quotes["volume"][i]) if quotes["volume"][i] else 0,
        })

    return {
        "symbol": meta.get("symbol", symbol),
        "name": meta.get("shortName", symbol),
        "market": "JP" if ".T" in symbol else "US",
        "currency": meta.get("currency", "USD"),
        "data": candles,
    }


def fetch_dividend_data(symbol: str) -> dict:
    """
    Yahoo Finance から配当情報を取得する
    複数のAPIエンドポイントを試行し、最初に成功したものを使用する

    Args:
        symbol: 銘柄シンボル（例: "AAPL", "7203.T"）

    Returns:
        dict: 配当情報
    """
    # 方法1: v10 quoteSummary API（最も詳細）
    try:
        return _fetch_dividend_v10(symbol)
    except Exception as e:
        print(f"  v10 API失敗 ({symbol}): {e}")

    # 方法2: v8 chart API のメタデータから取得（フォールバック）
    try:
        return _fetch_dividend_from_chart(symbol)
    except Exception as e:
        print(f"  chart API fallback失敗 ({symbol}): {e}")

    # すべて失敗した場合
    return {
        "symbol": symbol,
        "name": symbol,
        "exchange": "",
        "sector": "",
        "industry": "",
        "forwardDividend": 0,
        "forwardYield": 0,
        "trailingDividend": 0,
        "trailingYield": 0,
        "dividendRate": 0,
        "dividendYield": 0,
        "source": "none",
        "price": 0,
        "change": 0,
        "changePercent": 0,
        "currency": "JPY" if ".T" in symbol else "USD",
        "exDividendDate": "",
    }


def _fetch_dividend_v10(symbol: str) -> dict:
    """v10 quoteSummary APIから配当情報を取得"""
    base_url = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/"
    # ETFの場合assetProfileがないことがあるので、基本モジュールだけで試す
    modules = "summaryDetail,price,calendarEvents"
    params = urllib.parse.urlencode({"modules": modules})
    url = f"{base_url}{urllib.parse.quote(symbol)}?{params}"

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })

    with urllib.request.urlopen(req, timeout=10, context=ssl_context) as response:
        raw_data = json.loads(response.read().decode("utf-8"))

    result = raw_data["quoteSummary"]["result"][0]
    summary = result.get("summaryDetail", {})
    price_data = result.get("price", {})

    # assetProfile は追加リクエストで取得を試みる
    sector = ""
    industry = ""
    try:
        params2 = urllib.parse.urlencode({"modules": "assetProfile"})
        url2 = f"{base_url}{urllib.parse.quote(symbol)}?{params2}"
        req2 = urllib.request.Request(url2, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        with urllib.request.urlopen(req2, timeout=5, context=ssl_context) as resp2:
            raw2 = json.loads(resp2.read().decode("utf-8"))
        profile = raw2["quoteSummary"]["result"][0].get("assetProfile", {})
        sector = profile.get("sector", "")
        industry = profile.get("industry", "")
    except Exception:
        pass

    # Forward（予想）配当
    forward_rate = summary.get("dividendRate", {}).get("raw", 0)
    forward_yield = summary.get("dividendYield", {}).get("raw", 0)

    # Trailing（実績）配当
    trailing_rate = summary.get("trailingAnnualDividendRate", {}).get("raw", 0)
    trailing_yield = summary.get("trailingAnnualDividendYield", {}).get("raw", 0)

    # 現在の株価
    current_price = price_data.get("regularMarketPrice", {}).get("raw", 0)
    currency = price_data.get("currency", "USD")

    # 株価変動
    change = price_data.get("regularMarketChange", {}).get("raw", 0)
    change_percent = price_data.get("regularMarketChangePercent", {}).get("raw", 0)

    # 銘柄情報
    short_name = price_data.get("shortName", symbol)
    exchange = price_data.get("exchangeName", "")

    # 配当落ち日（次回）
    ex_div_date_raw = summary.get("exDividendDate", {}).get("raw", 0)
    ex_div_date = ""
    if ex_div_date_raw:
        try:
            ex_div_date = datetime.utcfromtimestamp(ex_div_date_raw).strftime("%Y-%m-%d")
        except Exception:
            pass

    # Forward配当が利用可能ならそちらを優先
    if forward_rate and forward_rate > 0:
        use_rate = forward_rate
        use_yield = forward_yield
        source = "forward"
    elif trailing_rate and trailing_rate > 0:
        use_rate = trailing_rate
        use_yield = trailing_yield
        source = "trailing"
    else:
        use_rate = 0
        use_yield = 0
        source = "none"

    return {
        "symbol": symbol,
        "name": short_name,
        "exchange": exchange,
        "sector": sector,
        "industry": industry,
        "forwardDividend": forward_rate,
        "forwardYield": round(forward_yield * 100, 2) if forward_yield else 0,
        "trailingDividend": trailing_rate,
        "trailingYield": round(trailing_yield * 100, 2) if trailing_yield else 0,
        "dividendRate": use_rate,
        "dividendYield": round(use_yield * 100, 2) if use_yield else 0,
        "source": source,
        "price": current_price,
        "change": round(change, 2) if change else 0,
        "changePercent": round(change_percent * 100, 2) if change_percent else 0,
        "currency": currency,
        "exDividendDate": ex_div_date,
    }


def _fetch_dividend_from_chart(symbol: str) -> dict:
    """v8 chart APIの配当イベントデータから配当情報を取得（フォールバック）

    events=div パラメータで過去2年分の配当履歴を取得し、
    直近1年分の配当金を合計して年間配当金額と利回りを計算する
    """
    base_url = "https://query1.finance.yahoo.com/v8/finance/chart/"
    params = urllib.parse.urlencode({
        "interval": "1d",
        "range": "2y",
        "includePrePost": "false",
        "events": "div",  # 配当イベントを含める
    })
    url = f"{base_url}{urllib.parse.quote(symbol)}?{params}"

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })

    with urllib.request.urlopen(req, timeout=10, context=ssl_context) as response:
        raw_data = json.loads(response.read().decode("utf-8"))

    chart = raw_data["chart"]["result"][0]
    meta = chart["meta"]

    short_name = meta.get("shortName", symbol)
    currency = meta.get("currency", "USD")
    current_price = meta.get("regularMarketPrice", 0)
    prev_close = meta.get("previousClose", 0)
    exchange = meta.get("exchangeName", "")

    # 株価変動: メタデータのpreviousCloseと現在価格から計算
    # previousCloseが0や同値の場合、実際のキャンドルデータから計算
    change = round(current_price - prev_close, 2) if prev_close else 0
    change_pct = round((change / prev_close) * 100, 2) if prev_close and prev_close != current_price else 0

    # メタデータで変動が0の場合、キャンドルデータから計算を試みる
    if change == 0 and "timestamp" in chart and chart["timestamp"]:
        quotes = chart.get("indicators", {}).get("quote", [{}])[0]
        closes = quotes.get("close", [])
        # 末尾からNoneでない直近2本のcloseを探す
        valid_closes = [c for c in closes if c is not None]
        if len(valid_closes) >= 2:
            today_c = valid_closes[-1]
            prev_c = valid_closes[-2]
            change = round(today_c - prev_c, 2)
            change_pct = round((change / prev_c) * 100, 2) if prev_c else 0
            current_price = today_c

    # 配当イベントを解析
    events = chart.get("events", {})
    dividends = events.get("dividends", {})

    annual_dividend = 0
    ex_div_date = ""
    latest_div_ts = 0

    if dividends:
        # 直近1年分の配当を合計
        one_year_ago = time.time() - (365 * 24 * 60 * 60)
        for ts_key, div_info in dividends.items():
            div_ts = int(ts_key)
            div_amount = div_info.get("amount", 0)
            if div_ts >= one_year_ago:
                annual_dividend += div_amount
            # 最新の配当落ち日を記録
            if div_ts > latest_div_ts:
                latest_div_ts = div_ts
                try:
                    ex_div_date = datetime.utcfromtimestamp(div_ts).strftime("%Y-%m-%d")
                except Exception:
                    pass

    # 配当利回りを計算
    dividend_yield = 0
    if annual_dividend > 0 and current_price > 0:
        dividend_yield = round((annual_dividend / current_price) * 100, 2)

    source = "trailing" if annual_dividend > 0 else "none"

    print(f"  chart配当データ ({symbol}): 年間配当={annual_dividend}, 利回り={dividend_yield}%, 配当落ち日={ex_div_date}")

    return {
        "symbol": symbol,
        "name": short_name,
        "exchange": exchange,
        "sector": "",
        "industry": "",
        "forwardDividend": 0,
        "forwardYield": 0,
        "trailingDividend": annual_dividend,
        "trailingYield": dividend_yield,
        "dividendRate": annual_dividend,
        "dividendYield": dividend_yield,
        "source": source,
        "price": current_price,
        "change": change,
        "changePercent": change_pct,
        "currency": currency,
        "exDividendDate": ex_div_date,
    }


def fetch_fundamentals_data(code: str) -> dict:
    """
    IR BANKからファンダメンタルズデータを取得する

    Args:
        code: 4桁の証券コード（例: "7203"）

    Returns:
        dict: ファンダメンタルズデータ（売上、営業利益率、EPS、営業CF、配当、配当性向、自己資本比率、現金等）
    """
    url = f"https://f.irbank.net/files/{code}/fy-data-all.json"

    # ファンダメンタルズ専用キャッシュを確認（スクリーニング等からの呼び出しにも対応）
    cache_key = f"fundamentals|{code}"
    cached_entry = _fundamentals_cache.get(cache_key)
    if cached_entry and (time.time() - cached_entry["timestamp"] < _FUNDAMENTALS_CACHE_TTL):
        print(f"  IR BANK ({code}): ファンダメンタルズキャッシュヒット（残り{int((_FUNDAMENTALS_CACHE_TTL - (time.time() - cached_entry['timestamp'])) / 3600)}時間）")
        return cached_entry["data"]

    if not _HAS_BROTLI:
        return {"code": code, "error": "brotliモジュールが必要です。ターミナルで pip install brotli を実行してください。", "sections": {}}

    # IR BANKへのアクセス間隔を制御（過剰アクセス防止）
    _wait_irbank_rate_limit()

    try:
        print(f"  IR BANK ({code}): データ取得開始... brotli={_HAS_BROTLI}")
        import http.cookiejar
        ir_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        }

        # Step 1: まずIR BANKのトップページにアクセスしてセッションCookieを取得
        cookie_jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookie_jar),
            urllib.request.HTTPSHandler(context=ssl_context)
        )

        top_req = urllib.request.Request(f"https://irbank.net/{code}", headers={
            **ir_headers,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        try:
            with opener.open(top_req, timeout=10) as resp:
                _ = resp.read()  # ページ内容は不要、Cookieだけ取得
                cookies_str = "; ".join([f"{c.name}={c.value}" for c in cookie_jar])
                print(f"  IR BANK ({code}): Cookie取得成功: {cookies_str[:80]}...")
        except Exception as cookie_err:
            print(f"  IR BANK ({code}): Cookie取得失敗: {cookie_err}")

        # Step 2: CookieをつけてJSON APIにアクセス
        json_req = urllib.request.Request(url, headers={
            **ir_headers,
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": f"https://irbank.net/{code}",
            "Origin": "https://irbank.net",
        })
        with opener.open(json_req, timeout=15) as response:
            raw_bytes = response.read()
            content_encoding = response.headers.get("Content-Encoding", "")
            print(f"  IR BANK ({code}): Content-Encoding={content_encoding}, size={len(raw_bytes)}bytes")

        # 圧縮形式に応じて解凍
        if content_encoding == "br" or (raw_bytes and raw_bytes[0:1] not in (b'[', b'{')):
            raw_bytes = brotli.decompress(raw_bytes)
        elif content_encoding == "gzip" or raw_bytes[:2] == b'\x1f\x8b':
            raw_bytes = gzip.decompress(raw_bytes)

        raw_data = json.loads(raw_bytes.decode("utf-8"))
    except Exception as e:
        print(f"  IR BANK取得エラー ({code}): {e}")
        return {"code": code, "error": f"IR BANKからデータを取得できません: {str(e)}", "sections": {}}

    # ==========================================
    # IR BANKのデータ形式:
    # {
    #   "業績": {"meta": {"item": {"年度": ["売上高","営業利益",...]}}, "item": {"2024/03": [値,...], ...}},
    #   "財務": {"meta": {...}, "item": {...}},
    #   "CF":   {"meta": {...}, "item": {...}},
    #   "配当": {"meta": {...}, "item": {...}}
    # }
    # ==========================================

    if not isinstance(raw_data, dict):
        print(f"  IR BANK ({code}): 予期しない型 {type(raw_data).__name__}")
        return {"code": code, "error": "データが見つかりません", "sections": {}}

    categories = list(raw_data.keys())
    print(f"  IR BANK ({code}): カテゴリ = {categories}")

    # ヘルパー: カテゴリからカラムのインデックスを探す
    def find_col_index(category_data, *col_names):
        """metaの年度リストから指定名のインデックスを返す（完全一致のみ）"""
        meta = category_data.get("meta", {})
        item_meta = meta.get("item", {})
        columns = item_meta.get("年度", [])
        # 完全一致
        for name in col_names:
            if name in columns:
                return columns.index(name)
        # 列名の中に検索名が含まれる場合のみ（逆はNG: 短い列名が長い検索名に含まれるケースを除外）
        for name in col_names:
            for i, col in enumerate(columns):
                if name in col and len(name) >= 3:  # 検索名が列名に含まれる場合のみ
                    return i
        return -1

    # ヘルパー: カテゴリから年度別の値を抽出
    def extract_series(category_data, col_index):
        """item辞書から年度順に値を抽出"""
        items = category_data.get("item", {})
        if col_index < 0 or not items:
            return [], []
        # 年度でソート
        sorted_periods = sorted(items.keys())
        periods = []
        values = []
        for period in sorted_periods:
            row = items[period]
            if isinstance(row, list) and col_index < len(row):
                val = row[col_index]
                # 文字列の数値を変換（"363994000000" → 数値）
                if isinstance(val, str):
                    try:
                        val = float(val) if "." in val else int(val)
                    except ValueError:
                        val = None
                periods.append(period)
                values.append(val)
            else:
                periods.append(period)
                values.append(None)
        return periods, values

    # 各カテゴリのメタ情報をログに出力
    for cat_name, cat_data in raw_data.items():
        if isinstance(cat_data, dict) and "meta" in cat_data:
            cols = cat_data.get("meta", {}).get("item", {}).get("年度", [])
            n_items = len(cat_data.get("item", {}))
            print(f"  IR BANK ({code}): [{cat_name}] 列名={cols}, データ数={n_items}")

    # マスタデータから銘柄名を取得
    stock_name = code
    for stock in JP_STOCKS:
        if stock["code"] == code:
            stock_name = stock["name"]
            break

    # 各セクションのマッピング定義
    # (セクションキー, ラベル, 単位, カテゴリ候補, 列名候補, 値変換)
    section_defs = [
        ("revenue",         "売上高",       "百万円", ["業績"],        ["売上高", "営業収益", "経常収益", "売上収益", "収益"],  "to_million"),
        ("operatingProfit", "営業利益",     "百万円", ["業績"],        ["営業利益"],                                             "to_million"),
        ("operatingMargin", "営業利益率",   "%",      ["業績"],        ["営業利益率", "営利率", "営業利益率(%)"],                None),
        ("eps",             "EPS",          "円",     ["業績"],        ["EPS", "1株益", "1株当たり利益", "一株益", "1株当り利益"], None),
        ("dividend",        "一株配当",     "円",     ["配当"],        ["1株配当", "1株配", "配当金", "一株配当", "1株当り配当"],  None),
        ("dividendYield",   "配当利回り",   "%",      ["配当"],        ["配当利回り", "配当利回り(%)"],                            None),
        ("payoutRatio",     "配当性向",     "%",      ["配当"],        ["配当性向", "配当性向(%)"],                              None),
        ("equityRatio",     "自己資本比率", "%",      ["財務"],        ["自己資本比率", "自己資本比率(%)"],                      None),
        ("roe",             "ROE",          "%",      ["財務"],        ["ROE", "自己資本利益率", "ROE(%)"],                      None),
        ("bps",             "BPS",          "円",     ["財務"],        ["BPS", "1株純資産", "1株当たり純資産", "一株純資産"],     None),
        ("operatingCF",     "営業CF",       "百万円", ["CF"],          ["営業CF", "営業活動によるCF"],                            "to_million"),
    ]

    # 期間リストを決定（全カテゴリの期間をマージ）
    period_set = set()
    for cat_name, cat_data in raw_data.items():
        if isinstance(cat_data, dict):
            items = cat_data.get("item", {})
            if items:
                cat_periods = list(items.keys())
                print(f"  IR BANK ({code}): [{cat_name}] 期間数={len(cat_periods)}, 先頭={cat_periods[:3]}, 末尾={cat_periods[-3:]}")
                period_set.update(cat_periods)
    all_periods = sorted(period_set)

    # 直近15年分に限定
    if len(all_periods) > 15:
        all_periods = all_periods[-15:]

    print(f"  IR BANK ({code}): 統合期間リスト ({len(all_periods)}件) = {all_periods}")

    result = {
        "code": code,
        "stockName": stock_name,
        "periods": all_periods,
        "sections": {}
    }

    # 各セクションのデータを抽出
    for sec_key, sec_label, sec_unit, cat_candidates, col_candidates, conversion in section_defs:
        found = False
        for cat_name in cat_candidates:
            if cat_name not in raw_data:
                continue
            cat_data = raw_data[cat_name]
            col_idx = find_col_index(cat_data, *col_candidates)
            if col_idx < 0:
                continue

            # マッチした列名をログ出力
            matched_col = cat_data.get("meta", {}).get("item", {}).get("年度", [])[col_idx] if col_idx >= 0 else "?"
            print(f"  IR BANK ({code}): {sec_key}: [{cat_name}] 列{col_idx}='{matched_col}'")

            # 値を抽出（all_periodsに合わせる）
            items = cat_data.get("item", {})
            values = []
            # 配当関連のデバッグ: 最初と最後の期間の生データを出力
            if sec_key in ("payoutRatio", "dividend"):
                dbg_periods = all_periods[:2] + all_periods[-2:]
                for dbg_p in dbg_periods:
                    dbg_row = items.get(dbg_p)
                    print(f"  IR BANK ({code}): [{cat_name}] {sec_key} {dbg_p}: row_len={len(dbg_row) if isinstance(dbg_row, list) else 'N/A'}, col_idx={col_idx}, raw_val={dbg_row[col_idx] if isinstance(dbg_row, list) and col_idx < len(dbg_row) else 'N/A'}")
            for period in all_periods:
                row = items.get(period)
                if row and isinstance(row, list) and col_idx < len(row):
                    val = row[col_idx]
                    # None, 空文字, "-" などはNone扱い
                    if val is None or val == "" or val == "-" or val == "－":
                        values.append(None)
                        continue
                    # 文字列→数値変換
                    if isinstance(val, str):
                        # カンマ除去
                        val = val.replace(",", "")
                        try:
                            val = float(val) if "." in val else int(val)
                        except (ValueError, OverflowError):
                            val = None
                    # 百万円単位に変換（元データが円単位の場合）
                    if conversion == "to_million" and val is not None and isinstance(val, (int, float)):
                        if abs(val) > 1_000_000_000:  # 10億以上なら円単位と判断
                            val = round(val / 1_000_000)
                    values.append(val)
                else:
                    values.append(None)

            non_null = [v for v in values if v is not None]
            print(f"  IR BANK ({code}): {sec_key}={sec_label}: [{cat_name}]列{col_idx} → {len(non_null)}/{len(values)}件有効")
            result["sections"][sec_key] = {
                "label": sec_label,
                "unit": sec_unit,
                "values": values,
            }
            found = True
            break

        if not found:
            print(f"  IR BANK ({code}): {sec_key}={sec_label}: 該当列なし")
            result["sections"][sec_key] = {
                "label": sec_label,
                "unit": sec_unit,
                "values": [None] * len(all_periods),
            }

    # =============================================
    # 営業利益率を計算（直接取得できなかった場合）
    # 営業利益率 = 営業利益 ÷ 売上高 × 100
    # ※ to_million変換後の値は単位が揃わない可能性があるため、
    #   IR BANKの生データから直接計算する
    # =============================================
    om = result["sections"].get("operatingMargin", {})
    om_values = om.get("values", [])
    if all(v is None for v in om_values):
        # IR BANKの生データから売上高と営業利益の列を探して直接計算
        calc_values = [None] * len(all_periods)
        calc_success = False
        def _to_num(v):
            """文字列を数値に変換するヘルパー"""
            if v is None or v == "" or v == "-" or v == "－":
                return None
            if isinstance(v, str):
                v = v.replace(",", "")
                try:
                    return float(v) if "." in v else int(v)
                except (ValueError, OverflowError):
                    return None
            return v

        if "業績" in raw_data:
            perf_data = raw_data["業績"]
            rev_idx = find_col_index(perf_data, "売上高", "営業収益", "経常収益", "売上収益", "収益")
            op_idx = find_col_index(perf_data, "営業利益")
            if rev_idx >= 0 and op_idx >= 0:
                items = perf_data.get("item", {})
                for i, period in enumerate(all_periods):
                    row = items.get(period)
                    if row and isinstance(row, list) and rev_idx < len(row) and op_idx < len(row):
                        r_num = _to_num(row[rev_idx])
                        o_num = _to_num(row[op_idx])
                        if r_num is not None and o_num is not None and r_num != 0:
                            calc_values[i] = round(o_num / r_num * 100, 2)
                calc_success = True
        non_null = [v for v in calc_values if v is not None]
        if calc_success and non_null:
            print(f"  IR BANK ({code}): operatingMargin: 生データから営業利益÷売上高で計算 → {len(non_null)}件有効")
            result["sections"]["operatingMargin"] = {
                "label": "営業利益率",
                "unit": "%",
                "values": calc_values,
            }

    # =============================================
    # 配当性向を計算（IR BANKのデータがほぼ「-」の場合）
    # 配当性向 = 一株配当 ÷ EPS × 100
    # =============================================
    pr = result["sections"].get("payoutRatio", {})
    pr_values = pr.get("values", [])
    pr_valid = [v for v in pr_values if v is not None]
    if len(pr_valid) < len(pr_values) * 0.5:  # 半分以上がNullなら計算で補完
        div_vals = result["sections"].get("dividend", {}).get("values", [])
        eps_vals = result["sections"].get("eps", {}).get("values", [])
        if div_vals and eps_vals and len(div_vals) == len(eps_vals):
            calc_pr = []
            for i in range(len(div_vals)):
                # 既存の値があればそれを使う
                existing = pr_values[i] if i < len(pr_values) else None
                if existing is not None:
                    calc_pr.append(existing)
                elif div_vals[i] is not None and eps_vals[i] is not None and eps_vals[i] != 0:
                    calc_pr.append(round(div_vals[i] / eps_vals[i] * 100, 2))
                else:
                    calc_pr.append(None)
            non_null = [v for v in calc_pr if v is not None]
            if len(non_null) > len(pr_valid):
                print(f"  IR BANK ({code}): payoutRatio: 1株配当÷EPSで計算補完 → {len(non_null)}件有効")
                result["sections"]["payoutRatio"] = {
                    "label": "配当性向",
                    "unit": "%",
                    "values": calc_pr,
                }

    # =============================================
    # ROEを計算（直接取得できなかった場合）
    # ROE = EPS ÷ BPS × 100
    # =============================================
    roe = result["sections"].get("roe", {})
    roe_values = roe.get("values", [])
    if all(v is None for v in roe_values):
        # BPSを探す
        bps_idx = -1
        for cat_name in ["財務"]:
            if cat_name in raw_data:
                bps_idx = find_col_index(raw_data[cat_name], "BPS", "1株純資産", "一株純資産")
                if bps_idx >= 0:
                    eps_vals = result["sections"].get("eps", {}).get("values", [])
                    bps_items = raw_data[cat_name].get("item", {})
                    calc_roe = []
                    for period in all_periods:
                        bps_row = bps_items.get(period)
                        bps_val = None
                        if bps_row and isinstance(bps_row, list) and bps_idx < len(bps_row):
                            bv = bps_row[bps_idx]
                            if bv is not None and bv != "" and bv != "-" and bv != "－":
                                if isinstance(bv, str):
                                    bv = bv.replace(",", "")
                                    try:
                                        bps_val = float(bv) if "." in bv else int(bv)
                                    except (ValueError, OverflowError):
                                        bps_val = None
                                else:
                                    bps_val = bv

                        idx = all_periods.index(period)
                        eps_v = eps_vals[idx] if idx < len(eps_vals) else None
                        if eps_v is not None and bps_val is not None and bps_val != 0:
                            calc_roe.append(round(eps_v / bps_val * 100, 2))
                        else:
                            calc_roe.append(None)

                    non_null = [v for v in calc_roe if v is not None]
                    if non_null:
                        print(f"  IR BANK ({code}): ROE: EPS÷BPSで計算 → {len(non_null)}件有効")
                        result["sections"]["roe"] = {
                            "label": "ROE",
                            "unit": "%",
                            "values": calc_roe,
                        }
                    break
        if bps_idx < 0:
            print(f"  IR BANK ({code}): ROE: BPSも見つからず計算不可")

    # =============================================
    # 配当利回りを計算（IR BANKのデータがない場合）
    # 配当利回り = 1株配当 ÷ 年度末株価 × 100
    # Yahoo Financeから過去の月次終値を取得して計算
    # =============================================
    dy = result["sections"].get("dividendYield", {})
    dy_values = dy.get("values", [])
    dy_valid = [v for v in dy_values if v is not None]
    if len(dy_valid) < len(dy_values) * 0.5:
        div_vals = result["sections"].get("dividend", {}).get("values", [])
        if div_vals and any(v is not None for v in div_vals):
            try:
                # 各期間の年度末月の終値を取得
                yearly_prices = _fetch_yearly_closing_prices(code, all_periods)
                if yearly_prices:
                    calc_dy = []
                    for i, period in enumerate(all_periods):
                        dv = div_vals[i] if i < len(div_vals) else None
                        price = yearly_prices.get(period)
                        # 既存の値があればそれを使う
                        existing = dy_values[i] if i < len(dy_values) else None
                        if existing is not None:
                            calc_dy.append(existing)
                        elif dv is not None and price is not None and price > 0:
                            dy_calc = round(dv / price * 100, 2)
                            print(f"    配当利回り計算 {period}: 配当={dv}円 ÷ 株価={price:.0f}円 × 100 = {dy_calc}%")
                            calc_dy.append(dy_calc)
                        else:
                            calc_dy.append(None)
                    non_null = [v for v in calc_dy if v is not None]
                    if len(non_null) > len(dy_valid):
                        print(f"  IR BANK ({code}): dividendYield: 1株配当÷株価で計算 → {len(non_null)}件有効")
                        result["sections"]["dividendYield"] = {
                            "label": "配当利回り",
                            "unit": "%",
                            "values": calc_dy,
                        }
            except Exception as e:
                print(f"  IR BANK ({code}): dividendYield計算エラー: {e}")

    # operatingProfitはフロントエンドで不要なので除外
    if "operatingProfit" in result["sections"]:
        del result["sections"]["operatingProfit"]

    # =============================================
    # Yahoo Financeからバリュエーション指標を取得
    # PER, PBR, 配当利回り
    # =============================================
    result["valuation"] = fetch_valuation_metrics(code)

    # ファンダメンタルズ専用キャッシュに保存（メモリ+ファイル）
    if not result.get("error"):
        _fundamentals_cache[f"fundamentals|{code}"] = {"data": result, "timestamp": time.time()}
        _save_fundamentals_cache()
        print(f"  IR BANK ({code}): ファンダメンタルズキャッシュに保存完了")

    return result


def _fetch_yearly_closing_prices(code: str, periods: list) -> dict:
    """
    Yahoo Financeから各年度末月の終値を取得する

    Args:
        code: 証券コード
        periods: 年度期間リスト (例: ["2015/03", "2016/03", ...])

    Returns:
        dict: {period: closing_price} の辞書
    """
    symbol = f"{code}.T"

    # 期間の範囲を計算（最古の期間の1ヶ月前から現在まで）
    if not periods:
        return {}

    # 最古の年度を取得
    first_period = periods[0]  # "2013/03" のような形式
    try:
        parts = first_period.split("/")
        first_year = int(parts[0])
        first_month = int(parts[1]) if len(parts) > 1 else 3
    except (ValueError, IndexError):
        first_year = 2013
        first_month = 1

    # period1: 最古期間の2ヶ月前
    import calendar
    period1_year = first_year
    period1_month = first_month - 2
    if period1_month <= 0:
        period1_month += 12
        period1_year -= 1
    period1_ts = int(calendar.timegm((period1_year, period1_month, 1, 0, 0, 0)))
    period2_ts = int(time.time())

    # 月次データを取得
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1mo&period1={period1_ts}&period2={period2_ts}&includePrePost=false"

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    })

    with urllib.request.urlopen(req, timeout=15, context=ssl_context) as response:
        data = json.loads(response.read().decode("utf-8"))

    chart = data["chart"]["result"][0]
    timestamps = chart.get("timestamp", [])
    closes = chart["indicators"]["quote"][0].get("close", [])

    # タイムスタンプ→年月の終値マップを構築
    monthly_closes = {}
    for i, ts in enumerate(timestamps):
        if i < len(closes) and closes[i] is not None:
            dt = datetime.utcfromtimestamp(ts)
            key = f"{dt.year}/{dt.month:02d}"
            monthly_closes[key] = closes[i]

    print(f"  Yahoo Finance ({code}): 月次終値 {len(monthly_closes)}件取得")

    # 各期間に対応する終値を抽出
    result = {}
    for period in periods:
        # periodは "2024/03" のような形式
        # その月の終値、なければ前月の終値を使う
        if period in monthly_closes:
            result[period] = monthly_closes[period]
        else:
            # 前月を試す
            try:
                parts = period.split("/")
                y, m = int(parts[0]), int(parts[1])
                prev_m = m - 1
                prev_y = y
                if prev_m <= 0:
                    prev_m = 12
                    prev_y -= 1
                prev_key = f"{prev_y}/{prev_m:02d}"
                if prev_key in monthly_closes:
                    result[period] = monthly_closes[prev_key]
                else:
                    # 翌月も試す
                    next_m = m + 1
                    next_y = y
                    if next_m > 12:
                        next_m = 1
                        next_y += 1
                    next_key = f"{next_y}/{next_m:02d}"
                    if next_key in monthly_closes:
                        result[period] = monthly_closes[next_key]
            except (ValueError, IndexError):
                pass

    print(f"  Yahoo Finance ({code}): 年度末終値マッチ {len(result)}/{len(periods)}件")
    return result


def fetch_valuation_metrics(code: str) -> dict:
    """
    Yahoo Financeからバリュエーション指標を取得する
    PER（予想）、PBR、配当利回り（実績）
    """
    symbol = f"{code}.T"
    url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}?modules=summaryDetail,defaultKeyStatistics,financialData"

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        })
        with urllib.request.urlopen(req, timeout=10, context=ssl_context) as response:
            data = json.loads(response.read().decode("utf-8"))

        result_data = data.get("quoteSummary", {}).get("result", [])
        if not result_data:
            print(f"  Yahoo Finance ({code}): quoteSummaryデータなし")
            return {}

        info = result_data[0]
        summary = info.get("summaryDetail", {})
        key_stats = info.get("defaultKeyStatistics", {})
        fin_data = info.get("financialData", {})

        def get_raw(obj, key):
            val = obj.get(key, {})
            if isinstance(val, dict):
                return val.get("raw")
            return val

        metrics = {}

        # 配当利回り（%）
        dy = get_raw(summary, "dividendYield")
        if dy is not None:
            metrics["dividendYield"] = round(dy * 100, 2)

        # 予想PER（Forward P/E）
        fpe = get_raw(summary, "forwardPE") or get_raw(key_stats, "forwardPE")
        if fpe is not None:
            metrics["forwardPER"] = round(fpe, 2)

        # 実績PER（Trailing P/E）
        tpe = get_raw(summary, "trailingPE") or get_raw(key_stats, "trailingPE")
        if tpe is not None:
            metrics["trailingPER"] = round(tpe, 2)

        # PBR（Price to Book）
        pb = get_raw(key_stats, "priceToBook") or get_raw(summary, "priceToBook")
        if pb is not None:
            metrics["pbr"] = round(pb, 2)

        print(f"  Yahoo Finance ({code}): バリュエーション = {metrics}")
        return metrics

    except Exception as e:
        import traceback
        print(f"  Yahoo Finance ({code}): バリュエーション取得エラー: {e}")
        traceback.print_exc()
        return {}


def search_stocks(query: str, market: str) -> list:
    """
    銘柄を検索する
    - 日本株: まずローカルの銘柄マスタから検索（名前・コードで部分一致）
    - 米国株: Yahoo Finance APIで検索
    - どちらもフォールバックとしてYahoo Finance APIを使う

    Args:
        query: 検索キーワード
        market: マーケット（"JP" or "US"）

    Returns:
        list: 検索結果リスト
    """
    # まずローカルマスタから検索
    if market == "JP" and JP_STOCKS:
        results = search_jp_local(query)
        if results:
            return results
    elif market == "US" and US_STOCKS:
        results = search_us_local(query)
        if results:
            return results

    # ローカルで見つからない場合、Yahoo Finance APIで検索
    try:
        return search_yahoo_finance(query, market)
    except Exception as e:
        print(f"  Yahoo Finance検索エラー: {e}")
        return []


def search_jp_local(query: str) -> list:
    """
    日本株をローカルの銘柄マスタから検索する
    証券コード（部分一致）または銘柄名（部分一致）で検索

    Args:
        query: 検索キーワード（例: "トヨタ", "7203", "銀行"）

    Returns:
        list: 検索結果リスト（最大10件）
    """
    query_lower = query.lower()
    results = []

    for stock in JP_STOCKS:
        code = stock["code"]
        name = stock["name"]
        sector = stock.get("sector", "")

        # コードの前方一致、または名前・セクターの部分一致
        match = (
            code.startswith(query) or
            query_lower in name.lower() or
            query_lower in sector.lower()
        )

        if match:
            results.append({
                "symbol": code,
                "yahooSymbol": f"{code}.T",
                "name": name,
                "market": "JP",
                "exchange": "TSE",
                "sector": sector,
            })

        if len(results) >= 10:
            break

    return results


def search_us_local(query: str) -> list:
    """
    米国株をローカルの銘柄マスタから検索する
    ティッカーシンボル（前方一致）または企業名・セクター（部分一致）で検索

    Args:
        query: 検索キーワード（例: "MSFT", "Apple", "Technology"）

    Returns:
        list: 検索結果リスト（最大10件）
    """
    query_upper = query.upper()
    query_lower = query.lower()
    results = []

    # 検索エイリアス（略称 → 実際のYahoo Financeシンボル）
    SEARCH_ALIASES = {
        "VIX": "^VIX",
        "US10Y": "^TNX",
        "10Y": "^TNX",
        "TNX": "^TNX",
        "USDJPY": "USDJPY=X",
        "ドル円": "USDJPY=X",
    }

    # エイリアスに一致した場合、クエリを実シンボルに変換
    alias_match = SEARCH_ALIASES.get(query_upper)

    def _make_result(stock):
        return {
            "symbol": stock["symbol"],
            "yahooSymbol": stock["symbol"],
            "name": stock["name"],
            "market": "US",
            "exchange": "US",
            "sector": stock.get("sector", ""),
        }

    # 0. エイリアス完全一致
    if alias_match:
        for stock in US_STOCKS:
            if stock["symbol"].upper() == alias_match.upper():
                results.append(_make_result(stock))
                break

    # 1. シンボル完全一致を最優先
    for stock in US_STOCKS:
        if stock["symbol"].upper() == query_upper:
            if not any(r["symbol"] == stock["symbol"] for r in results):
                results.append(_make_result(stock))
            break

    # 2. シンボル前方一致（完全一致・エイリアス一致は除く）
    for stock in US_STOCKS:
        if any(r["symbol"] == stock["symbol"] for r in results):
            continue
        if stock["symbol"].upper().startswith(query_upper):
            results.append(_make_result(stock))
        if len(results) >= 10:
            return results

    # 次に名前・セクターの部分一致
    for stock in US_STOCKS:
        # 既にシンボル一致で入っていたらスキップ
        if any(r["symbol"] == stock["symbol"] for r in results):
            continue

        name = stock["name"]
        sector = stock.get("sector", "")

        if query_lower in name.lower() or query_lower in sector.lower():
            results.append({
                "symbol": stock["symbol"],
                "yahooSymbol": stock["symbol"],
                "name": stock["name"],
                "market": "US",
                "exchange": "US",
                "sector": sector,
            })

        if len(results) >= 10:
            break

    return results


def search_yahoo_finance(query: str, market: str) -> list:
    """
    Yahoo Finance APIで銘柄を検索する

    Args:
        query: 検索キーワード
        market: マーケット（"JP" or "US"）

    Returns:
        list: 検索結果リスト
    """
    url = "https://query2.finance.yahoo.com/v1/finance/search"
    params = urllib.parse.urlencode({
        "q": query,
        "quotesCount": 10,
        "newsCount": 0,
    })
    full_url = f"{url}?{params}"

    req = urllib.request.Request(full_url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    })

    with urllib.request.urlopen(req, timeout=10, context=ssl_context) as response:
        raw_data = json.loads(response.read().decode("utf-8"))

    results = []
    for item in raw_data.get("quotes", []):
        # 株式のみをフィルタ（ETFや投信は除外しない）
        item_type = item.get("quoteType", "")
        if item_type not in ("EQUITY", "ETF"):
            continue

        symbol = item.get("symbol", "")
        exchange = item.get("exchange", "")

        # マーケットフィルタ
        if market == "JP" and not symbol.endswith(".T"):
            continue
        if market == "US" and symbol.endswith(".T"):
            continue

        results.append({
            "symbol": symbol.replace(".T", "") if symbol.endswith(".T") else symbol,
            "yahooSymbol": symbol,
            "name": item.get("shortname", item.get("longname", symbol)),
            "market": "JP" if symbol.endswith(".T") else "US",
            "exchange": exchange,
        })

    return results[:10]


def _background_screening_update():
    """
    バックグラウンドでスクリーニングデータを更新する。
    サーバー起動時に別スレッドで実行される。
    - キャッシュが30日以内 → 株価のみ更新（IR BANKアクセスなし）
    - キャッシュが30日超/ない → IR BANKからフル取得（3秒間隔）
    """
    import threading
    print(f"  [バックグラウンド更新] 開始 ({len(SCREENING_CANDIDATES)}銘柄)")

    success = 0
    price_only = 0
    errors = 0
    start = time.time()

    for i, candidate in enumerate(SCREENING_CANDIDATES):
        code = candidate["code"]

        # 既存キャッシュの確認
        cache_key = f"screening|{code}"
        cached = _screening_cache.get(cache_key)
        cache_age_hours = 999
        if cached:
            cache_age_hours = (time.time() - cached["timestamp"]) / 3600

        # 12時間以内のキャッシュがある → 株価のみ更新
        if cached and cache_age_hours < 720 and cached["data"].get("dividendType"):  # 30日 = 720時間
            try:
                symbol = f"{code}.T"
                div_data = fetch_dividend_data(symbol)
                price = div_data.get("price", 0)
                if price and price > 0:
                    d = dict(cached["data"])
                    d["price"] = price
                    div_annual = d.get("dividendAnnual", 0)
                    if div_annual and div_annual > 0:
                        d["dividendYield"] = round(div_annual / price * 100, 2)
                    eps = d.get("eps")
                    if eps and eps > 0:
                        d["per"] = round(price / eps, 2)
                    bps = d.get("bps")
                    if bps and bps > 0:
                        d["pbr"] = round(price / bps, 2)
                    d["lastUpdated"] = int(time.time())
                    _screening_cache[cache_key] = {"data": d, "timestamp": time.time()}
                    price_only += 1
            except Exception:
                pass
            continue

        # フル取得（IR BANKアクセスあり）
        try:
            data = _fetch_screening_data_static(code)
            if data and not data.get("_irbank_error"):
                _screening_cache[cache_key] = {"data": data, "timestamp": time.time()}
                success += 1
            else:
                errors += 1
        except Exception:
            errors += 1

        # 進捗ログ（50銘柄ごと）
        if (i + 1) % 50 == 0:
            elapsed = time.time() - start
            print(f"  [バックグラウンド更新] {i+1}/{len(SCREENING_CANDIDATES)} 処理済み（{elapsed:.0f}秒）")

        # 10銘柄ごとにキャッシュ保存
        if (i + 1) % 10 == 0:
            _save_screening_cache()

    # 最終保存
    _save_screening_cache()
    elapsed = time.time() - start
    print(f"  [バックグラウンド更新] 完了: フル取得={success}, 株価更新={price_only}, エラー={errors}（{elapsed:.0f}秒）")


def main():
    """サーバーを起動する"""
    # Renderなどのクラウド環境ではPORT環境変数が設定される
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), StockAPIHandler)

    print("=" * 50)
    print("  StockView サーバー起動")
    print(f"  http://localhost:{port}")
    print("=" * 50)
    print("  Ctrl+C で停止できます")
    print()

    # バックグラウンドでスクリーニングデータを更新
    import threading
    bg_thread = threading.Thread(target=_background_screening_update, daemon=True)
    bg_thread.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nサーバーを停止しました")
        server.server_close()


if __name__ == "__main__":
    main()
