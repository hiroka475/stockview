"""
StockView - 株価データAPIサーバー
Yahoo Financeから株価データを取得し、フロントエンドに提供する
"""

import json
import os
import urllib.request
import urllib.parse
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from datetime import datetime, timedelta

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


class StockAPIHandler(SimpleHTTPRequestHandler):
    """株価データAPIとフロントエンドの静的ファイルを配信するHTTPハンドラ"""

    def do_GET(self):
        """GETリクエストの処理"""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # APIルート
        if path.startswith("/api/dividend/"):
            self.handle_dividend_api(path, query)
        elif path.startswith("/api/stock/"):
            self.handle_stock_api(path, query)
        elif path.startswith("/api/search"):
            self.handle_search_api(query)
        else:
            # 静的ファイル（HTML/CSS/JS）を配信
            super().do_GET()

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
            data = fetch_yahoo_finance(yahoo_symbol, interval, date_range)
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
            data = fetch_dividend_data(yahoo_symbol)
            self.send_json_response(data)
        except Exception as e:
            self.send_json_error(f"配当データ取得エラー: {str(e)}", 500)

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

    with urllib.request.urlopen(req, timeout=10) as response:
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
    Yahoo Finance の quoteSummary API から配当情報を取得する

    Args:
        symbol: 銘柄シンボル（例: "AAPL", "7203.T"）

    Returns:
        dict: 配当情報
            - forwardDividend: 予想年間配当金（来期）
            - forwardYield: 予想配当利回り（来期）
            - trailingDividend: 実績年間配当金（過去12ヶ月）
            - trailingYield: 実績配当利回り（過去12ヶ月）
            - dividendRate: 使用する配当金額（forward優先）
            - dividendYield: 使用する配当利回り（forward優先）
            - source: "forward" or "trailing"
            - price: 現在の株価
    """
    base_url = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/"
    params = urllib.parse.urlencode({
        "modules": "summaryDetail,price,calendarEvents,assetProfile",
    })
    url = f"{base_url}{urllib.parse.quote(symbol)}?{params}"

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    })

    with urllib.request.urlopen(req, timeout=10) as response:
        raw_data = json.loads(response.read().decode("utf-8"))

    result = raw_data["quoteSummary"]["result"][0]
    summary = result.get("summaryDetail", {})
    price_data = result.get("price", {})
    calendar = result.get("calendarEvents", {})
    profile = result.get("assetProfile", {})

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
    sector = profile.get("sector", "")
    industry = profile.get("industry", "")

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

    # まずシンボルの前方一致を優先
    for stock in US_STOCKS:
        if stock["symbol"].upper().startswith(query_upper):
            results.append({
                "symbol": stock["symbol"],
                "yahooSymbol": stock["symbol"],
                "name": stock["name"],
                "market": "US",
                "exchange": "US",
                "sector": stock.get("sector", ""),
            })
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

    with urllib.request.urlopen(req, timeout=10) as response:
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
