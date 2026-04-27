import { NextRequest, NextResponse } from "next/server";
import { searchStocks } from "@/lib/yahoo";


export async function GET(req: NextRequest) {
  const q = req.nextUrl.searchParams.get("q") ?? "";
  const market = req.nextUrl.searchParams.get("market") ?? "US";
  if (!q) return NextResponse.json({ results: [] });
  try {
    const results = await searchStocks(q, market);
    return NextResponse.json({ results });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
