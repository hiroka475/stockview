import { NextRequest, NextResponse } from "next/server";
import { getEnv } from "@/lib/cloudflare";
import { fetchDividendData } from "@/lib/yahoo";


export async function POST(req: NextRequest) {
  try {
    const body = await req.json() as { codes?: string[] };
    const codes = body.codes ?? [];
    const env = getEnv();

    const raw = await env.KV.get("screening-cache");
    const cache: Record<string, Record<string, unknown>> = raw ? JSON.parse(raw) : {};

    let updatedCount = 0;
    const errors: unknown[] = [];

    for (const code of codes) {
      try {
        const divData = await fetchDividendData(`${code}.T`);
        const price = divData.price ?? 0;
        if (!price || price <= 0) continue;

        const existing = cache[code];
        if (!existing) continue;

        existing.price = price;
        const div = existing.dividendAnnual as number;
        if (div && div > 0) existing.dividendYield = Math.round((div / price) * 10000) / 100;
        const eps = existing.eps as number;
        if (eps && eps > 0) existing.per = Math.round((price / eps) * 100) / 100;
        const bps = existing.bps as number;
        if (bps && bps > 0) existing.pbr = Math.round((price / bps) * 100) / 100;
        existing.lastUpdated_yahoo = Math.floor(Date.now() / 1000);
        existing.lastUpdated = Math.floor(Date.now() / 1000);
        cache[code] = existing;
        updatedCount++;
      } catch (e) {
        errors.push({ code, error: String(e) });
      }
    }

    if (updatedCount > 0) {
      await env.KV.put("screening-cache", JSON.stringify(cache));
    }

    return NextResponse.json({ updated: updatedCount, errors });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
