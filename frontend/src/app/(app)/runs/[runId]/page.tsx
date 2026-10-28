import { Suspense } from "react";

import { RunDetail } from "@/features/runs/RunDetail";

export default function Page() {
  return (
    <Suspense fallback={<p role="status">Loading run...</p>}>
      <RunDetail />
    </Suspense>
  );
}
