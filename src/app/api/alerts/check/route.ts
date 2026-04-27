import { NextResponse } from "next/server";
import { getEnv } from "@/lib/cloudflare";
import { fetchYahooFinance } from "@/lib/yahoo";


type AlertInfo = {
  enabled: boolean;
  price: number;
  direction: "below" | "above";
  name?: string;
};

export async function POST() {
  try {
    const env = getEnv();
    const raw = await env.KV.get("alerts");
    const alerts: Record<string, AlertInfo> = raw ? JSON.parse(raw) : {};

    const triggered: unknown[] = [];

    for (const [symbol, info] of Object.entries(alerts)) {
      if (!info.enabled) continue;
      try {
        const code = symbol.replace(".T", "");
        const yahooSymbol = /^\d{4}$/.test(code) ? `${code}.T` : symbol;
        const data = await fetchYahooFinance(yahooSymbol, "1d", "5d");
        const candles = data.data;
        if (!candles.length) continue;
        const currentPrice = candles[candles.length - 1].close ?? 0;
        const isTriggered =
          info.direction === "below"
            ? currentPrice <= info.price
            : currentPrice >= info.price;
        if (isTriggered) {
          triggered.push({
            symbol,
            name: info.name ?? code,
            alertPrice: info.price,
            currentPrice,
            direction: info.direction,
          });
        }
      } catch {
        continue;
      }
    }

    return NextResponse.json({ triggered });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
