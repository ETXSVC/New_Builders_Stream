export async function GET() {
  try {
    const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/health`, { cache: "no-store" });
    if (!res.ok) {
      return Response.json({ backend: `error: backend responded with ${res.status}` }, { status: 502 });
    }
    const body = await res.json();
    return Response.json({ backend: body.status });
  } catch {
    return Response.json({ backend: "unreachable" }, { status: 502 });
  }
}
