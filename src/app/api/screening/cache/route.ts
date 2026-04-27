import { NextResponse } from "next/server";
import { getEnv } from "@/lib/cloudflare";


export async function GET() {
  try {
    const env = getEnv();
    const raw = await env.KV.get("screening-cache");
    return NextResponse.json(raw ? JSON.parse(raw) : {});
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
