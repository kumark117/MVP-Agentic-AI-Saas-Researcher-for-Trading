import { Suspense } from "react";

import { ResearchEntry } from "@/features/research/ResearchEntry";
import { LatestRuns } from "@/features/runs/RunList";

export default function Page() {
  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-semibold">Research</h1>
      <Suspense fallback={<p role="status">Loading...</p>}>
        <ResearchEntry />
      </Suspense>
      <LatestRuns />
    </div>
  );
}
