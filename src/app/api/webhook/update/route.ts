import { NextRequest, NextResponse } from "next/server";
import { getEnv } from "@/lib/cloudflare";


// Webhookトリガー: スクリーニングキャッシュの更新
// Cloudflare Workers では外部スクリプト実行が不可のため、
// キャッシュのクリア（次回アクセス時に再取得させる）を行う
export async function POST(req: NextRequest) {
  const key = req.nextUrl.searchParams.get("key") ?? "";
  const mode = req.nextUrl.searchParams.get("mode") ?? "yahoo";
  const env = getEnv();

  const expectedKey = env.WEBHOOK_SECRET ?? "";
  if (!expectedKey || key !== expectedKey) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  if (!["yahoo", "irbank", "full"].includes(mode)) {
    return NextResponse.json({ error: "Invalid mode" }, { status: 400 });
  }

  try {
    if (mode === "full" || mode === "irbank") {
      await env.KV.delete("screening-cache");
    }
    return NextResponse.json({ status: "cache_cleared", mode });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
