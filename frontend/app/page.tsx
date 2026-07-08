async function getBackendHealth(): Promise<string> {
  try {
    const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/health`, { cache: "no-store" });
    if (!res.ok) return `backend responded with ${res.status}`;
    const body = await res.json();
    return body.status;
  } catch {
    return "unreachable";
  }
}

export default async function Home() {
  const backendStatus = await getBackendHealth();
  return (
    <main>
      <h1>Builders Stream</h1>
      <p>Backend status: {backendStatus}</p>
    </main>
  );
}
