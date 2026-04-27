import { NextRequest, NextResponse } from "next/server";
import { getEnv } from "@/lib/cloudflare";


export async function POST(req: NextRequest) {
  try {
    const alerts = await req.json();
    const env = getEnv();
    await env.KV.put("alerts", JSON.stringify(alerts));
    return NextResponse.json({ status: "ok" });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
