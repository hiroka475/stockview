import { NextRequest, NextResponse } from "next/server";


export async function GET(req: NextRequest) {
  // dividend-candidates.json は /public に置かれており、同一オリジンから取得
  const origin = req.nextUrl.origin;
  try {
    const res = await fetch(`${origin}/dividend-candidates.json`);
    if (!res.ok) return NextResponse.json({ error: "候補リストが見つかりません" }, { status: 404 });
    const candidates = await res.json() as Array<Record<string, unknown>>;

    // 規模区分を正規化
    for (const c of candidates) {
      const size = (c.size as string) ?? "";
      if (size.includes("Large") || size.includes("Core30")) c.size = "大型株";
      else if (size.includes("Mid") || size.includes("400")) c.size = "中型株";
      else if (size.includes("Small")) c.size = "小型株";
      else c.size = "";
    }

    return NextResponse.json(candidates);
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
