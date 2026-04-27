import { NextRequest, NextResponse } from "next/server";
import { fetchFundamentalsData } from "@/lib/irbank";
import { getEnv } from "@/lib/cloudflare";


const SOFT_TTL = 7 * 24 * 3600;   // 7日
const HARD_TTL = 30 * 24 * 3600;  // 30日

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ code: string }> }
) {
  const { code: rawCode } = await params;
  const code = decodeURIComponent(rawCode).replace(".T", "");
  if (!/^\d{4}$/.test(code)) {
    return NextResponse.json({ error: "日本株の4桁コードを指定してください" }, { status: 400 });
  }

  const env = getEnv();
  const kvKey = `fundamentals:v4:${code}`;

  // KVキャッシュを確認
  const cached = await env.KV.get(kvKey);
  if (cached) {
    const entry = JSON.parse(cached) as { data: Record<string, unknown>; timestamp: number };
    const age = Date.now() / 1000 - entry.timestamp;

    if (age < SOFT_TTL) {
      // 新鮮なキャッシュ
      return NextResponse.json(entry.data);
    }

    if (age < HARD_TTL) {
      // やや古い: キャッシュを即返し、バックグラウンドで更新
      // Cloudflare Workers では waitUntil で非同期更新
      try {
        const ctx = (await import("@opennextjs/cloudflare")).getCloudflareContext();
        ctx.ctx.waitUntil(
          fetchFundamentalsData(code).then((data) =>
            env.KV.put(kvKey, JSON.stringify({ data, timestamp: Date.now() / 1000 }), {
              expirationTtl: HARD_TTL * 2,
            })
          )
        );
      } catch {
        // ローカル開発環境では waitUntil が使えないので無視
      }
      return NextResponse.json(entry.data);
    }
  }

  // キャッシュなし or 期限切れ: 新規取得
  try {
    const data = await fetchFundamentalsData(code);
    if (!(data as { error?: string }).error) {
      await env.KV.put(kvKey, JSON.stringify({ data, timestamp: Date.now() / 1000 }), {
        expirationTtl: HARD_TTL * 2,
      });
    }
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json({ error: String(e), code, sections: {} }, { status: 500 });
  }
}
