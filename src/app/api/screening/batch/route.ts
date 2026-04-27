import { NextRequest, NextResponse } from "next/server";
import { getEnv } from "@/lib/cloudflare";


// スクリーニングのbatchはKVキャッシュから返すのみ
// IR BANKからのライブ取得は /api/fundamentals/[code] 経由で行う
export async function POST(req: NextRequest) {
  try {
    const body = await req.json() as { codes?: string[] };
    const codes = body.codes ?? [];
    const env = getEnv();

    const raw = await env.KV.get("screening-cache");
    const cache: Record<string, unknown> = raw ? JSON.parse(raw) : {};

    const results: Record<string, unknown> = {};
    let cachedCount = 0;

    for (const code of codes) {
      if (cache[code]) {
        results[code] = cache[code];
        cachedCount++;
      }
    }

    return NextResponse.json({
      results,
      cached: cachedCount,
      fetched: 0,
      errors: [],
    });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
