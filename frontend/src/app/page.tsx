import { API_BASE } from "@/lib/api/config";

// Phase 0 placeholder. The research dashboard replaces it in phase 7.
export default function Home() {
  return (
    <main className="mx-auto flex w-full max-w-2xl flex-1 flex-col gap-4 px-6 py-16">
      <h1 className="text-3xl font-semibold">AlloyIQ</h1>
      <p>AI research for metals markets. Research only: no orders, no real payments.</p>
      <p className="text-sm opacity-75">
        Bootstrap build. API health:{" "}
        <a className="underline" href={`${API_BASE}/health/live`}>
          {API_BASE}/health/live
        </a>
      </p>
    </main>
  );
}
