import { NextResponse } from "next/server";
import { getEnv } from "@/lib/cloudflare";


export async function GET() {
  const env = getEnv();
  const raw = await env.KV.get("alerts");
  return NextResponse.json(raw ? JSON.parse(raw) : {});
}
