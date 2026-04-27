import { NextRequest, NextResponse } from "next/server";
import { fetchYahooFinance } from "@/lib/yahoo";


export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ symbol: string }> }
) {
  const { symbol: rawSymbol } = await params;
  const symbol = decodeURIComponent(rawSymbol);
  const interval = req.nextUrl.searchParams.get("interval") ?? "1d";
  const range = req.nextUrl.searchParams.get("range") ?? "1y";

  const yahooSymbol =
    /^\d{4}$/.test(symbol) ? `${symbol}.T` : symbol;

  try {
    const data = await fetchYahooFinance(yahooSymbol, interval, range);
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
