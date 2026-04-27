import { NextRequest, NextResponse } from "next/server";
import { fetchDividendData } from "@/lib/yahoo";


export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ symbol: string }> }
) {
  const { symbol: rawSymbol } = await params;
  const symbol = decodeURIComponent(rawSymbol);
  const code = symbol.replace(".T", "");
  const yahooSymbol =
    /^\d{4}$/.test(code) && !symbol.endsWith(".T")
      ? `${code}.T`
      : symbol;

  try {
    const data = await fetchDividendData(yahooSymbol);
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
