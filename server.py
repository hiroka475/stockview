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
from http.server import HTTPServer, SimpleHTTPRequestHandler
from datetime import datetime, timedelta

# Render等のクラウド環境ではstdoutバッファリングを無効化（ログ即時表示のため）
if os.environ.get("RENDER") or os.environ.get("PORT"):
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

print("[起動] StockView サーバーを初期化中...")

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
            timeout=30,
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


# ファンダメンタルズ専用キャッシュ（長期保持: 30日間 + ファイル永続化）
# IR BANKのデータは決算発表時（年4回）しか変わらないため30日キャッシュで十分。
_fundamentals_cache = {}
_FUNDAMENTALS_CACHE_TTL = 2592000  # 30日間（秒）
_FUNDAMENTALS_CACHE_FILE = os.path.join(_base_dir, "fundamentals-cache.json")

# IR BANKへのアクセス間隔制御（過剰アクセス防止）
_last_irbank_access = 0
_IRBANK_ACCESS_INTERVAL = 3  # 最低3秒間隔

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
        elif path == "/api/screening/cache":
            self.handle_screening_cache_get()
        elif path == "/api/env":
            self.handle_env_api()
        elif path == "/" and os.environ.get("DEFAULT_PAGE") == "screening":
            # スクリーニング専用モード: / → screening.html にリダイレクト
            self.send_response(302)
            self.send_header("Location", "/screening.html")
            self.end_headers()
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
        elif path == "/api/screening/update-yahoo":
            self.handle_screening_update_yahoo()
        elif path == "/api/webhook/update":
            self.handle_webhook_update()
        else:
            self.send_json_error("Not Found", 404)


    # ==========================================
    # スクリーニング関連API
    # ==========================================

    def _get_screening_cache_path(self):
        """screening-cache.jsonのパスを返す"""
        return os.path.join(_base_dir, "screening-cache.json")

    def _load_screening_cache(self):
        """screening-cache.jsonを読み込む"""
        path = self._get_screening_cache_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[スクリーニング] キャッシュ読み込みエラー: {e}")
        return {}

    def _save_screening_cache(self, cache):
        """screening-cache.jsonに保存する"""
        path = self._get_screening_cache_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[スクリーニング] キャッシュ保存エラー: {e}")

    def handle_screening_candidates(self):
        """GET /api/screening/candidates - 候補銘柄リストを返す"""
        candidates_path = os.path.join(_base_dir, "dividend-candidates.json")
        if os.path.exists(candidates_path):
            try:
                with open(candidates_path, "r", encoding="utf-8") as f:
                    candidates = json.load(f)
                # 規模区分を正規化
                for c in candidates:
                    size = c.get("size", "")
                    if "Large" in size or "Core30" in size:
                        c["size"] = "大型株"
                    elif "Mid" in size or "400" in size:
                        c["size"] = "中型株"
                    elif "Small" in size:
                        c["size"] = "小型株"
                    else:
                        c["size"] = ""
                self.send_json_response(candidates)
            except Exception as e:
                self.send_json_error(f"候補リスト読み込みエラー: {e}")
        else:
            self.send_json_error("dividend-candidates.json が見つかりません", 404)

    def handle_screening_cache_get(self):
        """GET /api/screening/cache - キャッシュ済みデータをそのまま返す（外部アクセスなし）"""
        try:
            cache = self._load_screening_cache()
            self.send_json_response(cache)
        except Exception as e:
            self.send_json_error(f"キャッシュ読み込みエラー: {e}")

    def handle_screening_batch(self):
        """POST /api/screening/batch - バッチでスクリーニングデータを返す"""
        try:
            body = self._read_body_json()
            codes = body.get("codes", [])
            force = body.get("force", False)

            cache = self._load_screening_cache()
            results = {}
            cached_count = 0
            fetched_count = 0
            errors = []

            for code in codes:
                if not force and code in cache:
                    # キャッシュから返す
                    results[code] = cache[code]
                    cached_count += 1
                else:
                    # キャッシュになければIR BANK + Yahoo Financeから取得を試みる
                    try:
                        data = self._fetch_screening_data_static(code)
                        if data:
                            results[code] = data
                            cache[code] = data
                            fetched_count += 1
                        elif code in cache:
                            # 取得失敗時は既存キャッシュを使う
                            results[code] = cache[code]
                            cached_count += 1
                    except Exception as e:
                        print(f"[スクリーニング] {code} 取得エラー: {e}")
                        errors.append({"code": code, "error": str(e)})
                        if code in cache:
                            results[code] = cache[code]
                            cached_count += 1

            # キャッシュを保存（新しいデータがあれば）
            if fetched_count > 0:
                self._save_screening_cache(cache)

            self.send_json_response({
                "results": results,
                "cached": cached_count,
                "fetched": fetched_count,
                "errors": errors,
            })

        except Exception as e:
            print(f"[スクリーニング] バッチ処理エラー: {e}")
            self.send_json_error(f"バッチ処理エラー: {e}")

    def handle_screening_update_yahoo(self):
        """POST /api/screening/update-yahoo - Yahoo Financeから株価のみ更新（IR BANKアクセスなし）"""
        try:
            body = self._read_body_json()
            codes = body.get("codes", [])

            cache = self._load_screening_cache()
            updated_count = 0
            errors = []

            for code in codes:
                try:
                    symbol = f"{code}.T"
                    div_data = fetch_dividend_data(symbol)
                    price = div_data.get("price", 0)
                    if not price or price <= 0:
                        continue

                    existing = cache.get(code, {})
                    if not existing:
                        # キャッシュに存在しない銘柄は株価だけでは作れない→スキップ
                        continue

                    existing["price"] = price

                    # 既存のEPS/BPS/配当で PER/PBR/利回りを再計算
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
                    updated_count += 1

                except Exception as e:
                    errors.append({"code": code, "error": str(e)})

            if updated_count > 0:
                self._save_screening_cache(cache)

            self.send_json_response({
                "updated": updated_count,
                "errors": errors,
            })

        except Exception as e:
            print(f"[スクリーニング] Yahoo更新エラー: {e}")
            self.send_json_error(f"Yahoo更新エラー: {e}")

    def _fetch_screening_data_static(self, code):
        """
        1銘柄分のスクリーニングデータを取得する
        株価: Yahoo Finance / それ以外: IR BANK (fetch_fundamentals_data経由)
        """
        try:
            # Yahoo Financeから株価を取得
            symbol = f"{code}.T"
            div_data = fetch_dividend_data(symbol)
            price = div_data.get("price", 0)
            if not price or price <= 0:
                return None

            # IR BANKからファンダメンタルズを取得
            fund_data = fetch_fundamentals_data(code)
            if not fund_data or "sections" not in fund_data:
                return None

            sections = fund_data["sections"]
            result = {
                "code": code,
                "name": fund_data.get("stockName", code),
                "price": price,
                "lastUpdated": int(time.time()),
            }

            # 配当（予想優先）
            div_val, div_type = _get_latest_value_with_type(sections.get("dividend"))
            result["dividendAnnual"] = div_val
            result["dividendType"] = div_type
            if price > 0 and div_val and div_val > 0:
                result["dividendYield"] = round(div_val / price * 100, 2)
            else:
                result["dividendYield"] = 0

            # EPS（予想優先）
            eps_val, eps_type = _get_latest_value_with_type(sections.get("eps"))
            result["eps"] = eps_val
            result["epsType"] = eps_type

            # BPS（予想優先）
            bps_val, bps_type = _get_latest_value_with_type(sections.get("bps"))
            result["bps"] = bps_val
            result["bpsType"] = bps_type

            # PER = 株価 ÷ EPS
            if eps_val and eps_val > 0:
                result["per"] = round(price / eps_val, 2)
                result["perType"] = "予想" if eps_type == "予想" else "実績"
            else:
                result["per"] = None
                result["perType"] = None

            # PBR = 株価 ÷ BPS
            if bps_val and bps_val > 0:
                result["pbr"] = round(price / bps_val, 2)
                result["pbrType"] = "予想" if bps_type == "予想" else "実績"
            else:
                result["pbr"] = None
                result["pbrType"] = None

            # 営業利益率
            margin_val, _ = _get_latest_value_with_type(sections.get("operatingMargin"))
            result["operatingMargin"] = margin_val

            # ROE
            roe_val, _ = _get_latest_value_with_type(sections.get("roe"))
            result["roe"] = roe_val

            # 自己資本比率
            equity_val, _ = _get_latest_value_with_type(sections.get("equityRatio"))
            result["equityRatio"] = equity_val

            # 配当性向
            payout_val, _ = _get_latest_value_with_type(sections.get("payoutRatio"))
            result["payoutRatio"] = payout_val

            return result

        except Exception as e:
            print(f"[スクリーニング] {code} データ取得失敗: {e}")
            return None

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

    def handle_env_api(self):
        """GET /api/env - 環境情報を返す（Render上かどうかの判定用）"""
        is_render = bool(os.environ.get("RENDER"))
        self.send_json_response({"isCloud": is_render})

    def handle_webhook_update(self):
        """POST /api/webhook/update?key=SECRET&mode=yahoo|irbank - 外部cronからの更新トリガー"""
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        # 認証チェック
        key = query.get("key", [""])[0]
        expected_key = os.environ.get("WEBHOOK_SECRET", "")
        if not expected_key or key != expected_key:
            self.send_json_error("Unauthorized", 401)
            return

        mode = query.get("mode", ["yahoo"])[0]
        if mode not in ("yahoo", "irbank", "full"):
            self.send_json_error("Invalid mode. Use yahoo, irbank, or full.", 400)
            return

        # バックグラウンドでupdate-screening-data.pyを実行
        script_path = os.path.join(_base_dir, "update-screening-data.py")
        cmd = [sys.executable, script_path, "--mode", mode]
        if mode == "irbank":
            cmd += ["--batch-size", "800"]

        try:
            log_dir = os.path.join(_base_dir, "logs")
            os.makedirs(log_dir, exist_ok=True)
            subprocess.Popen(
                cmd,
                cwd=_base_dir,
                stdout=open(os.path.join(log_dir, "webhook-update.log"), "a"),
                stderr=subprocess.STDOUT,
            )
            print(f"[Webhook] {mode}モードの更新を開始しました")
            self.send_json_response({"status": "started", "mode": mode})
        except Exception as e:
            print(f"[Webhook] 更新スクリプトの起動エラー: {e}")
            self.send_json_error(f"Failed to start update: {e}", 500)

    def log_message(self, format, *args):
        """ログメッセージを日本語フレンドリーにする"""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]}")


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


def _get_latest_value_with_type(section):
    """
    セクションの values から直近の非null値を取得し、「予想」か「実績」かを判定する

    Args:
        section: {"values": [...], "label": "...", "unit": "..."} 形式のdict

    Returns:
        (value, type_str): 値と "予想" or "実績" のタプル。データがなければ (None, None)
    """
    if not section or "values" not in section:
        return None, None

    values = section["values"]
    if not values:
        return None, None

    # 末尾から非null値を探す（最新データは配列の末尾に近い）
    for i in range(len(values) - 1, -1, -1):
        if values[i] is not None:
            # 最後の値が予想かどうか: 末尾付近の値は予想値の可能性が高い
            # 判定: 最後から2番目以降に非null値があり、それが最後の非null値と
            # 同じインデックスなら実績、最後のインデックスなら予想の可能性
            is_forecast = (i == len(values) - 1)
            type_str = "予想" if is_forecast else "実績"
            return values[i], type_str

    return None, None


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

    # 配当落ち日（次回 - 今日以降のみ表示、過去なら予測）
    ex_div_date_raw = summary.get("exDividendDate", {}).get("raw", 0)
    ex_div_date = ""
    if ex_div_date_raw:
        try:
            ex_date = datetime.utcfromtimestamp(ex_div_date_raw)
            if ex_date.date() >= datetime.now().date():
                ex_div_date = ex_date.strftime("%Y-%m-%d")
            else:
                # 過去の日付の場合、その月を基に次回を予測
                ex_month = ex_date.month
                today = datetime.now()
                # 同月で今年or来年の予測
                if today.month <= ex_month:
                    pred_year = today.year
                else:
                    pred_year = today.year + 1
                ex_div_date = f"{pred_year}-{ex_month:02d}（予想）"
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


def _predict_next_ex_div_date(dividends: dict) -> str:
    """過去の配当イベントから次回の配当落ち日を予測する

    過去の配当月パターンを分析し、今日以降で最も近い月の25日頃を返す。
    例: 過去に3月と9月に配当がある場合、次の3月または9月を予測。
    """
    if not dividends:
        return ""

    # 過去の配当月を収集
    months = []
    for ts_key in dividends.keys():
        try:
            dt = datetime.utcfromtimestamp(int(ts_key))
            months.append(dt.month)
        except Exception:
            continue

    if not months:
        return ""

    # 配当月のパターンを特定（重複を除いてソート）
    unique_months = sorted(set(months))

    # 今日の日付から次の配当月を探す
    today = datetime.now()
    current_month = today.month
    current_year = today.year

    for offset in range(1, 13):  # 最大12ヶ月先まで探す
        check_month = ((current_month - 1 + offset) % 12) + 1
        check_year = current_year + ((current_month - 1 + offset) // 12)
        if check_month in unique_months:
            # その月の下旬（25日頃）を予測日とする
            return f"{check_year}-{check_month:02d}"

    return ""


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
        now_ts = time.time()
        future_div_ts = 0  # 未来の配当落ち日を別途追跡
        for ts_key, div_info in dividends.items():
            div_ts = int(ts_key)
            div_amount = div_info.get("amount", 0)
            if div_ts >= one_year_ago:
                annual_dividend += div_amount
            # 最新の配当落ち日を記録（過去の最新）
            if div_ts > latest_div_ts:
                latest_div_ts = div_ts
            # 未来の配当落ち日（今日以降で最も近い日付）
            if div_ts >= now_ts and (future_div_ts == 0 or div_ts < future_div_ts):
                future_div_ts = div_ts

        # 未来の配当落ち日があればそれを表示、なければ過去データから予測
        if future_div_ts > 0:
            try:
                ex_div_date = datetime.utcfromtimestamp(future_div_ts).strftime("%Y-%m-%d")
            except Exception:
                pass
        else:
            # 過去の配当月パターンから次回を予測
            predicted = _predict_next_ex_div_date(dividends)
            if predicted:
                ex_div_date = predicted + "（予想）"

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

    # 末尾の全セクションがNoneの期間を除去（まだ決算データがない年度）
    while result["periods"]:
        last_idx = len(result["periods"]) - 1
        all_none = True
        for sec in result["sections"].values():
            vals = sec.get("values", [])
            if last_idx < len(vals) and vals[last_idx] is not None:
                all_none = False
                break
        if all_none:
            removed = result["periods"].pop()
            for sec in result["sections"].values():
                vals = sec.get("values", [])
                if vals:
                    vals.pop()
            print(f"  IR BANK ({code}): 末尾の空期間を除去: {removed}")
        else:
            break

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

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nサーバーを停止しました")
        server.server_close()


if __name__ == "__main__":
    main()
