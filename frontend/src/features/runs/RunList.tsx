"use client";

import Link from "next/link";

import { Card, DemoMoney, Duration, ErrorText, Timestamp } from "@/components/ui";
import { useRuns } from "@/lib/api/queries";

export function LatestRuns() {
  const runs = useRuns({}, 5);
  return (
    <Card title="Latest runs">
      <ErrorText error={runs.error} />
      {runs.data?.items.length === 0 && <p className="text-sm">No research runs yet.</p>}
      <ul className="flex flex-col gap-2">
        {runs.data?.items.map((r) => (
          <li key={r.run_id} className="flex flex-wrap gap-x-4 text-sm">
            <Link className="underline" href={`/runs/${r.run_id}`}>
              {r.instrument_display_name} · {r.horizon.replace("_", " ")}
            </Link>
            <span>Status: {r.status.replace("_", " ")}</span>
            {r.signal && <span>Signal: {r.signal}</span>}
            <span>
              <Timestamp iso={r.submitted_at} />
            </span>
            <span>
              Took <Duration ms={r.end_to_end_ms} />
            </span>
            {r.settled_charge && (
              <span>
                Charge <DemoMoney value={r.settled_charge} />
              </span>
            )}
          </li>
        ))}
      </ul>
    </Card>
  );
}
