const COMMON_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
};

// ========================
// 株価チャートデータ
// ========================
export async function fetchYahooFinance(
  symbol: string,
  interval: string,
  range: string
) {
  const params = new URLSearchParams({
    interval,
    range,
    includePrePost: "false",
    events: "div",
  });
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}?${params}`;
  const res = await fetch(url, { headers: COMMON_HEADERS });
  if (!res.ok) throw new Error(`Yahoo Finance error: ${res.status}`);
  const raw = await res.json() as {
    chart: { result: Array<{
      meta: Record<string, unknown>;
      timestamp: number[];
      indicators: { quote: Array<{ open: number[]; high: number[]; low: number[]; close: number[]; volume: number[] }> };
      events?: { dividends?: Record<string, { amount: number; date: number }> };
    }> };
  };

  const result = raw.chart?.result?.[0];
  if (!result) throw new Error("データが見つかりません");

  const meta = result.meta;
  const timestamps = result.timestamp ?? [];
  const quote = result.indicators?.quote?.[0] ?? {};
  const dividends = result.events?.dividends ?? {};

  const candles = timestamps
    .map((ts, i) => ({
      date: new Date(ts * 1000).toISOString().slice(0, 10),
      time: ts,
      open: quote.open?.[i] != null ? Math.round(quote.open[i] * 100) / 100 : null,
      high: quote.high?.[i] != null ? Math.round(quote.high[i] * 100) / 100 : null,
      low: quote.low?.[i] != null ? Math.round(quote.low[i] * 100) / 100 : null,
      close: quote.close?.[i] != null ? Math.round(quote.close[i] * 100) / 100 : null,
      volume: quote.volume?.[i] ?? 0,
    }))
    // close が null、high/low/open が 0(Yahoo の確定前データ等)
    // または high が low の2倍超(株式分割等の不整合データ)のローソクを除外
    .filter((c) => {
      if (c.close === null || (c.high ?? 0) <= 0 || (c.low ?? 0) <= 0 || (c.open ?? 0) <= 0) return false;
      if (c.high !== null && c.low !== null && c.high > c.low * 2) return false;
      return true;
    });

  const currency = meta.currency as string ?? "JPY";
  const currentPrice = meta.regularMarketPrice as number ?? 0;
  // regularMarketChangePercent はリアルタイムの前日比（詳細パネルと同じ値）
  const rawChangePct = meta.regularMarketChangePercent as number;
  const rawChange = meta.regularMarketChange as number;
  const prevClose = meta.chartPreviousClose as number ?? 0;
  const changePct = rawChangePct != null ? rawChangePct * 100
    : prevClose ? ((currentPrice - prevClose) / prevClose) * 100 : 0;
  const change = rawChange != null ? rawChange
    : prevClose ? currentPrice - prevClose : 0;

  return {
    symbol,
    name: meta.shortName as string ?? symbol,
    exchange: meta.exchangeName as string ?? "",
    currency,
    currentPrice,
    change: Math.round(change * 100) / 100,
    changePercent: Math.round(changePct * 100) / 100,
    data: candles,
    dividends,
  };
}

// ========================
// 配当データ（v10 quoteSummary）
// ========================
import { fetchFundamentalsData } from "./irbank";

// 月末営業日（土日のみ考慮、祝日は無視）
function lastBusinessDayOfMonth(year: number, month1to12: number): Date {
  const d = new Date(Date.UTC(year, month1to12, 0));
  while (d.getUTCDay() === 0 || d.getUTCDay() === 6) {
    d.setUTCDate(d.getUTCDate() - 1);
  }
  return d;
}

// 直前の営業日（土日のみ考慮）
function previousBusinessDay(date: Date): Date {
  const d = new Date(date);
  d.setUTCDate(d.getUTCDate() - 1);
  while (d.getUTCDay() === 0 || d.getUTCDay() === 6) {
    d.setUTCDate(d.getUTCDate() - 1);
  }
  return d;
}

// 月末配当の権利落ち日（権利確定日 = 月末営業日、権利落ち日 = その1営業日前）
function exDividendDateForMonth(year: number, month1to12: number): Date {
  return previousBusinessDay(lastBusinessDayOfMonth(year, month1to12));
}

// 日本株の次回配当落ち日を IR Bank の決算期間から推定
// periods: ["2024/03", "2025/03", "2026/03"] のような配列を想定
// 中間配当（決算月-6）と期末配当（決算月）の2回前提
function computeNextExDividendJp(periods: string[] | undefined, today: Date): string {
  if (!periods || periods.length === 0) return "";
  const months = periods
    .map((p) => {
      const m = p.match(/^\d{4}\/(\d{2})$/);
      return m ? parseInt(m[1], 10) : null;
    })
    .filter((m): m is number => m !== null && m >= 1 && m <= 12);
  if (months.length === 0) return "";
  const fyEnd = months[months.length - 1];
  let interim = fyEnd - 6;
  if (interim <= 0) interim += 12;

  const todayUtc = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()));
  const candidates: Date[] = [];
  for (let yearOffset = 0; yearOffset <= 1; yearOffset++) {
    const y = todayUtc.getUTCFullYear() + yearOffset;
    candidates.push(exDividendDateForMonth(y, interim));
    candidates.push(exDividendDateForMonth(y, fyEnd));
  }
  candidates.sort((a, b) => a.getTime() - b.getTime());
  const next = candidates.find((d) => d.getTime() >= todayUtc.getTime());
  if (!next) return "";
  return next.toISOString().slice(0, 10) + "（予想）";
}

// IR Bank から日本株の配当データを取得してYahoo形式にマッピング
async function fetchDividendFromIrBank(symbol: string) {
  const code = symbol.replace(/\.T$/, "");
  if (!/^\d{4}$/.test(code)) return null;

  // 価格を Yahoo チャートAPIから取得
  let price = 0, change = 0, changePct = 0, currency = "JPY", shortName = symbol, exchange = "";
  try {
    const chart = await fetchYahooFinance(symbol, "1d", "5d");
    price = chart.currentPrice;
    change = chart.change;
    changePct = chart.changePercent;
    currency = chart.currency;
    shortName = chart.name;
    exchange = chart.exchange;
  } catch {
    // Yahoo失敗でも続行
  }

  const data = await fetchFundamentalsData(code) as {
    sections?: {
      dividend?: { values: (number | null)[] };
      dividendYield?: { values: (number | null)[] };
    };
    periods?: string[];
    dividendForecast?: (number | null)[];
  };

  const divVals = data.sections?.dividend?.values ?? [];
  const yieldVals = data.sections?.dividendYield?.values ?? [];
  const forecastVals = data.dividendForecast ?? [];

  // 最新の有効な配当値（dl.gdlの年間合計を優先、なければ予想テーブルの値を使用）
  const latestForecast = [...forecastVals].reverse().find((v) => v !== null && v !== undefined) ?? 0;
  const latestActual = [...divVals].reverse().find((v) => v !== null && v !== undefined) ?? 0;
  const latestDividend = latestActual > 0 ? latestActual : latestForecast;
  const latestYieldPct = [...yieldVals].reverse().find((v) => v !== null && v !== undefined) ?? 0;

  // 現在価格があれば利回りを再計算、なければIR Bankの値を利用
  const computedYield = price > 0 && latestDividend > 0 ? (latestDividend / price) * 100 : latestYieldPct;

  return {
    symbol,
    name: shortName,
    exchange,
    sector: "",
    industry: "",
    forwardDividend: latestDividend,
    forwardYield: Math.round(computedYield * 100) / 100,
    trailingDividend: latestDividend,
    trailingYield: Math.round(computedYield * 100) / 100,
    dividendRate: latestDividend,
    dividendYield: Math.round(computedYield * 100) / 100,
    source: latestDividend > 0 ? "irbank" : "none",
    price,
    change: Math.round(change * 100) / 100,
    changePercent: Math.round(changePct * 100) / 100,
    currency,
    exDividendDate: computeNextExDividendJp(data.periods, new Date()),
  };
}

// IR Bankから年間配当金（円）と次回配当落ち日（予想）を取得する
async function fetchIrBankDividendRate(code: string): Promise<{ rate: number; exDate: string; debug: string }> {
  try {
    const data = await fetchFundamentalsData(code) as {
      periods?: string[];
      sections?: { dividend?: { values: (number | null)[] } };
      dividendForecast?: (number | null)[];
      error?: string;
    };
    if (data.error) return { rate: 0, exDate: "", debug: `irbank_error:${data.error}` };
    const divVals = data.sections?.dividend?.values ?? [];
    const forecastVals = data.dividendForecast ?? [];
    const latestForecast = ([...forecastVals].reverse().find((v) => v != null) ?? 0) as number;
    const latestActual = ([...divVals].reverse().find((v) => v != null) ?? 0) as number;
    const rate = latestActual > 0 ? latestActual : latestForecast;
    const exDate = computeNextExDividendJp(data.periods, new Date());
    return { rate, exDate, debug: `irbank_ok:div=${JSON.stringify(divVals.slice(-3))},fc=${JSON.stringify(forecastVals.slice(-3))}` };
  } catch (e) {
    return { rate: 0, exDate: "", debug: `irbank_throw:${String(e)}` };
  }
}

export async function fetchDividendData(symbol: string) {
  const isJP = symbol.endsWith(".T");
  const modules = "summaryDetail,price,calendarEvents,assetProfile";
  const url = `https://query1.finance.yahoo.com/v10/finance/quoteSummary/${encodeURIComponent(symbol)}?modules=${modules}`;

  // 日本株はv10と並行してIR Bankの配当データも取得（v10のtrailingは不正確なケースがある）
  const irBankDivPromise = isJP
    ? fetchIrBankDividendRate(symbol.replace(".T", ""))
    : Promise.resolve({ rate: 0, exDate: "", debug: "not_jp" });

  try {
    const res = await fetch(url, { headers: COMMON_HEADERS });
    if (!res.ok) throw new Error(`Yahoo v10 error: ${res.status}`);
    const raw = await res.json() as {
      quoteSummary: { result: Array<Record<string, unknown>> };
    };
    const result = raw.quoteSummary?.result?.[0];
    if (!result) throw new Error("データなし");

    const summary = result.summaryDetail as Record<string, { raw?: number }> ?? {};
    const priceData = result.price as Record<string, { raw?: number } | string> ?? {};
    const profile = result.assetProfile as Record<string, string> ?? {};

    const getraw = (obj: Record<string, { raw?: number }>, key: string): number => {
      const v = obj[key];
      return (v && typeof v === "object" ? v.raw : undefined) ?? 0;
    };
    const getrawP = (key: string): number => {
      const v = (priceData as Record<string, { raw?: number }>)[key];
      return (v && typeof v === "object" ? v.raw : undefined) ?? 0;
    };

    const forwardRate = getraw(summary, "dividendRate");
    const forwardYield = getraw(summary, "dividendYield");
    const trailingRate = getraw(summary, "trailingAnnualDividendRate");
    const trailingYield = getraw(summary, "trailingAnnualDividendYield");
    const currentPrice = getrawP("regularMarketPrice");
    const change = getrawP("regularMarketChange");
    const changePct = getrawP("regularMarketChangePercent");
    const currency = (priceData.currency as string) ?? "JPY";
    const shortName = (priceData.shortName as string) ?? symbol;
    const exchange = (priceData.exchangeName as string) ?? "";

    const exRaw = getraw(summary, "exDividendDate");
    let exDivDate = "";
    if (exRaw) {
      const d = new Date(exRaw * 1000);
      const today = new Date();
      if (d >= today) {
        exDivDate = d.toISOString().slice(0, 10);
      } else {
        const m = d.getMonth() + 1;
        const y = today.getMonth() + 1 <= m ? today.getFullYear() : today.getFullYear() + 1;
        exDivDate = `${y}-${String(m).padStart(2, "0")}（予想）`;
      }
    }

    let useRate = forwardRate > 0 ? forwardRate : trailingRate;
    let useYield = forwardRate > 0 ? forwardYield : trailingYield;
    let source = forwardRate > 0 ? "forward" : trailingRate > 0 ? "trailing" : "none";

    // 日本株: IR Bankの年間配当で補正（v10のtrailingは不正確なケースがある）
    // また、Yahooが exDividendDate を返さない日本株は IR Bankの決算月から推定
    let irDebug = "skipped";
    if (isJP) {
      const irResult = await irBankDivPromise;
      irDebug = irResult.debug;
      if (forwardRate === 0 && irResult.rate > 0) {
        useRate = irResult.rate;
        useYield = currentPrice > 0 ? irResult.rate / currentPrice : 0;
        source = "irbank";
      }
      if (!exDivDate && irResult.exDate) {
        exDivDate = irResult.exDate;
      }
    }

    return {
      symbol,
      name: shortName,
      exchange,
      sector: profile.sector ?? "",
      industry: profile.industry ?? "",
      forwardDividend: forwardRate,
      forwardYield: Math.round(forwardYield * 10000) / 100,
      trailingDividend: trailingRate,
      trailingYield: Math.round(trailingYield * 10000) / 100,
      dividendRate: useRate,
      dividendYield: Math.round(useYield * 10000) / 100,
      source,
      price: currentPrice,
      change: Math.round(change * 100) / 100,
      changePercent: Math.round(changePct * 10000) / 100,
      currency,
      exDividendDate: exDivDate,
      _debug: irDebug,
    };
  } catch {
    // 日本株はIR Bankにフォールバック
    if (symbol.endsWith(".T") || /^\d{4}$/.test(symbol)) {
      const irData = await fetchDividendFromIrBank(symbol);
      if (irData) return irData;
    }
    return emptyDividend(symbol);
  }
}

function emptyDividend(symbol: string) {
  return {
    symbol, name: symbol, exchange: "", sector: "", industry: "",
    forwardDividend: 0, forwardYield: 0, trailingDividend: 0, trailingYield: 0,
    dividendRate: 0, dividendYield: 0, source: "none",
    price: 0, change: 0, changePercent: 0,
    currency: symbol.endsWith(".T") ? "JPY" : "USD",
    exDividendDate: "",
  };
}

// ========================
// 銘柄検索
// ========================
export async function searchStocks(q: string, market: string) {
  const url = `https://query2.finance.yahoo.com/v1/finance/search?q=${encodeURIComponent(q)}&newsCount=0&quotesCount=10&lang=ja&region=JP`;
  const res = await fetch(url, { headers: COMMON_HEADERS });
  if (!res.ok) return [];
  const data = await res.json() as { quotes?: Array<Record<string, unknown>> };
  const quotes = data.quotes ?? [];

  return quotes
    .filter((q) => {
      const type = q.quoteType as string ?? "";
      if (!["EQUITY", "ETF", "MUTUALFUND", "INDEX"].includes(type)) return false;
      const sym = q.symbol as string ?? "";
      if (market === "JP") return sym.endsWith(".T") || /^\d{4}\.T$/.test(sym);
      if (market === "US") return !sym.includes(".");
      return true;
    })
    .slice(0, 10)
    .map((q) => ({
      symbol: q.symbol,
      name: q.longname ?? q.shortname ?? q.symbol,
      exchange: q.exchange,
      type: q.quoteType,
    }));
}
