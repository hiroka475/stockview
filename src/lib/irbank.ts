// IR BANK からファンダメンタルズデータを取得する（HTMLスクレイピング版）

const IR_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
  "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
};

// 日本語の金額表記（兆・億・万）を円に変換
function parseJapaneseYen(text: string): number | null {
  if (!text) return null;
  // HTMLエンティティとスパンを除去
  const cleaned = text.replace(/<[^>]+>/g, "").replace(/&[a-z]+;/g, "").trim();
  if (!cleaned || cleaned === "-" || cleaned === "－" || cleaned === "赤字") return null;

  // パーセントは別関数で処理
  if (cleaned.endsWith("%")) return null;

  let negative = false;
  let s = cleaned;
  if (s.startsWith("-") || s.startsWith("▲")) {
    negative = true;
    s = s.replace(/^[-▲]/, "").trim();
  }

  let result = 0;
  const choMatch = s.match(/^([\d.]+)兆/);
  if (choMatch) {
    result += parseFloat(choMatch[1]) * 1e12;
    s = s.slice(choMatch[0].length);
  }
  const okuMatch = s.match(/^([\d.]+)億/);
  if (okuMatch) {
    result += parseFloat(okuMatch[1]) * 1e8;
    s = s.slice(okuMatch[0].length);
  }
  const manMatch = s.match(/^([\d.]+)万/);
  if (manMatch) {
    result += parseFloat(manMatch[1]) * 1e4;
    s = s.slice(manMatch[0].length);
  }
  const plainMatch = s.match(/^[\d.]+/);
  if (plainMatch) result += parseFloat(plainMatch[0]);

  if (result === 0 && !choMatch && !okuMatch && !manMatch && !plainMatch) return null;
  return negative ? -result : result;
}

// テキストから数値を取得（パーセント・円単位など）
function parseNumber(text: string): number | null {
  if (!text) return null;
  const cleaned = text.replace(/<[^>]+>/g, "").replace(/&[a-z]+;/g, "").trim();
  if (!cleaned || cleaned === "-" || cleaned === "－" || cleaned === "赤字") return null;

  const s = cleaned.replace(/[%円,*]/g, "").replace(/^▲/, "-").trim();
  const n = parseFloat(s);
  return isNaN(n) ? null : n;
}

// 百万円に変換
function toMillion(yen: number): number {
  return Math.round(yen / 1_000_000);
}

// <dt>タグから期間文字列を抽出（YYY/MM形式、または"2010年3月"形式）
function extractPeriod(dtContent: string): string {
  const text = dtContent.replace(/<[^>]+>/g, "").replace(/&[a-z]+;/g, "").trim();
  const slashMatch = text.match(/(\d{4})\/(\d{1,2})/);
  if (slashMatch) return `${slashMatch[1]}/${slashMatch[2].padStart(2, "0")}`;
  const jpMatch = text.match(/(\d{4})年(\d{1,2})月/);
  if (jpMatch) return `${jpMatch[1]}/${jpMatch[2].padStart(2, "0")}`;
  return "";
}

type SectionConfig = {
  labels: string[]; // 優先順に複数ラベル（業種によって異なる）
  key: string;
  unit: string;
  isYen: boolean;
};

const SECTION_MAP: SectionConfig[] = [
  { labels: ["売上高", "営業収益", "経常収益", "売上収益", "収益"],                    key: "revenue",         unit: "百万円", isYen: true  },
  { labels: ["営業利益"],                                                               key: "operatingProfit", unit: "百万円", isYen: true  },
  { labels: ["営業利益率"],                                                             key: "operatingMargin", unit: "%",      isYen: false },
  { labels: ["EPS"],                                                                    key: "eps",             unit: "円",     isYen: false },
  { labels: ["一株配当", "1株配当"],                                                    key: "dividend",        unit: "円",     isYen: false },
  { labels: ["配当利回り"],                                                             key: "dividendYield",   unit: "%",      isYen: false },
  { labels: ["配当性向"],                                                               key: "payoutRatio",     unit: "%",      isYen: false },
  { labels: ["株主資本比率", "自己資本比率"],                                           key: "equityRatio",     unit: "%",      isYen: false },
  { labels: ["ROE（自己資本利益率）", "ROE"],                                          key: "roe",             unit: "%",      isYen: false },
  { labels: ["BPS"],                                                                    key: "bps",             unit: "円",     isYen: false },
  { labels: ["営業活動によるCF", "営業CF"],                                            key: "operatingCF",     unit: "百万円", isYen: true  },
];

// dl.gdl要素から期間→値のマップを生成
function parseDlGdl(dlContent: string, isYen: boolean): Map<string, number | null> {
  const result = new Map<string, number | null>();
  // dt+dd ペアを抽出
  const pairPattern = /<dt>([\s\S]*?)<\/dt>\s*<dd>([\s\S]*?)<\/dd>/g;
  let m: RegExpExecArray | null;
  while ((m = pairPattern.exec(dlContent)) !== null) {
    const period = extractPeriod(m[1]);
    if (!period) continue;
    // Strip all HTML tags to get raw text (handles nested spans like <span class="co_red">*</span>48円)
    const valText = m[2].replace(/<[^>]+>/g, "").replace(/&[a-z]+;/g, "").trim();
    let val: number | null;
    if (isYen) {
      const yen = parseJapaneseYen(valText);
      val = yen !== null ? toMillion(yen) : null;
    } else {
      val = parseNumber(valText);
    }
    result.set(period, val);
  }
  return result;
}

// HTML から h2ラベル→dl.gdl内容 のマップを抽出
function extractDlSections(html: string): Map<string, string> {
  const dlMap = new Map<string, string>();
  // <dl class="gdl"> または <dl class="gdl xxx"> にマッチ
  const dlPattern = /<dl class="gdl[^"]*">/g;
  let m: RegExpExecArray | null;
  while ((m = dlPattern.exec(html)) !== null) {
    const dlIdx = m.index;
    const dlStart = dlIdx + m[0].length;
    const before = html.slice(Math.max(0, dlIdx - 400), dlIdx);
    const h2Matches = [...before.matchAll(/<h2[^>]*>([\s\S]*?)<\/h2>/g)];
    if (h2Matches.length === 0) continue;
    const lastH2 = h2Matches[h2Matches.length - 1];
    const labelRaw = lastH2[1].replace(/<[^>]+>/g, "").replace(/&[a-z]+;/g, "").replace(/#\d+/g, "").trim();
    const dlEnd = html.indexOf("</dl>", dlStart);
    if (dlEnd > dlStart) {
      // 既存ラベルがある場合はスキップ（/results のほうを優先）
      if (!dlMap.has(labelRaw)) {
        dlMap.set(labelRaw, html.slice(dlStart, dlEnd));
      }
    }
  }
  return dlMap;
}

// 配当予想テーブルから期間→予想値のマップを作る
// 予想/修正/実績 の優先順位: 実績 > 修正 > 予想
// 実績がない期間（未来）のみ予想値を返す
function extractDividendForecasts(html: string): Map<string, number> {
  const result = new Map<string, number>();
  const rowPattern = /<tr><td[^>]*rowspan="[^"]+"[^>]*>(\d{4})年<br>(\d{1,2})月<\/td>([\s\S]*?)(?=<tr><td[^>]*rowspan|<\/tbody>)/g;
  const typeValPattern = /<span class="co_[a-z]+">(予想|修正|実績)<\/span><\/td><td class="rt[^"]*">([^<]+)<\/td>/g;
  let m: RegExpExecArray | null;
  while ((m = rowPattern.exec(html)) !== null) {
    const period = `${m[1]}/${m[2].padStart(2, "0")}`;
    const block = m[3];
    let hasActual = false;
    let forecast: number | null = null;
    let revised: number | null = null;
    let t: RegExpExecArray | null;
    typeValPattern.lastIndex = 0;
    while ((t = typeValPattern.exec(block)) !== null) {
      const v = parseNumber(t[2]);
      if (v === null) continue;
      if (t[1] === "実績") hasActual = true;
      else if (t[1] === "修正") revised = v;
      else if (t[1] === "予想") forecast = v;
    }
    if (!hasActual) {
      const val = revised ?? forecast;
      if (val !== null) result.set(period, val);
    }
  }
  return result;
}

async function fetchIrBankHtml(path: string): Promise<string | null> {
  try {
    const res = await fetch(`https://irbank.net/${path}`, {
      headers: {
        ...IR_HEADERS,
        Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      },
      redirect: "follow",
    });
    if (!res.ok) return null;
    return await res.text();
  } catch {
    return null;
  }
}

export async function fetchFundamentalsData(code: string): Promise<Record<string, unknown>> {
  // /results と /dividend を並列取得
  const [resultsHtml, dividendHtml] = await Promise.all([
    fetchIrBankHtml(`${code}/results`),
    fetchIrBankHtml(`${code}/dividend`),
  ]);

  if (!resultsHtml && !dividendHtml) {
    return { code, error: `IR BANKからデータを取得できませんでした`, sections: {} };
  }

  // 両ページから dl セクションを収集してマージ
  const dlMap = new Map<string, string>();
  if (resultsHtml) {
    for (const [k, v] of extractDlSections(resultsHtml)) dlMap.set(k, v);
  }
  if (dividendHtml) {
    for (const [k, v] of extractDlSections(dividendHtml)) {
      if (!dlMap.has(k)) dlMap.set(k, v);
    }
  }

  // 全期間を収集
  const periodSet = new Set<string>();
  const sectionData = new Map<string, Map<string, number | null>>();

  for (const cfg of SECTION_MAP) {
    let dlContent: string | undefined;
    for (const lbl of cfg.labels) {
      dlContent = dlMap.get(lbl);
      if (dlContent) break;
    }
    if (!dlContent) continue;
    const parsed = parseDlGdl(dlContent, cfg.isYen);
    sectionData.set(cfg.key, parsed);
    for (const period of parsed.keys()) periodSet.add(period);
  }

  if (periodSet.size === 0) {
    return { code, error: "データが見つかりません", sections: {} };
  }

  // 配当予想を抽出（未来期間のみ）
  const forecasts = dividendHtml ? extractDividendForecasts(dividendHtml) : new Map<string, number>();
  // 配当予想の期間も全期間リストに加える
  for (const p of forecasts.keys()) periodSet.add(p);

  const rawPeriods = Array.from(periodSet).sort();

  // 主要決算月を推定（最頻出の月）→ 中間決算等のノイズ期間を除外
  // 例: 3月決算企業に紛れ込む 2025/12 のような中間期間を弾く
  const monthCount = new Map<string, number>();
  for (const p of rawPeriods) {
    const m = p.match(/\d{4}\/(\d{2})/);
    if (m) monthCount.set(m[1], (monthCount.get(m[1]) ?? 0) + 1);
  }
  let dominantMonth = "";
  let maxCount = 0;
  for (const [mm, cc] of monthCount) {
    if (cc > maxCount) { maxCount = cc; dominantMonth = mm; }
  }
  const allPeriods = dominantMonth
    ? rawPeriods.filter((p) => {
        const m = p.match(/\d{4}\/(\d{2})/);
        return !m || m[1] === dominantMonth;
      })
    : rawPeriods;

  const sections: Record<string, { label: string; unit: string; values: (number | null)[] }> = {};
  for (const cfg of SECTION_MAP) {
    const data = sectionData.get(cfg.key);
    if (!data) continue;
    sections[cfg.key] = {
      label: cfg.labels[0],
      unit: cfg.unit,
      values: allPeriods.map((p) => data.get(p) ?? null),
    };
  }

  // 配当予想を別フィールドで提供（期間ごとに actual がない場合のみ）
  const dividendForecast = allPeriods.map((p) => forecasts.get(p) ?? null);

  return { code, periods: allPeriods, sections, dividendForecast };
}

// スクリーニング用: 最新値を取得
export function getLatestValue(section: { values: (number | null)[]; label?: string; unit?: string } | undefined): number | null {
  if (!section) return null;
  const vals = [...section.values].reverse();
  return vals.find((v) => v !== null) ?? null;
}
