"use client";

/**
 * Research entry: plain, re-run (?rerun=<run>) or follow-up (?followup=<run>).
 * Both prefill a *new* estimate from the earlier run; a follow-up also continues its
 * conversation, and the API only allows it once that run is terminal (R033).
 */
import { useSearchParams } from "next/navigation";

import { Alert } from "@/components/ui";
import { useRun } from "@/lib/api/queries";

import { ResearchForm } from "./ResearchForm";

export function ResearchEntry() {
  const params = useSearchParams();
  const sourceId = params.get("followup") ?? params.get("rerun");
  const isFollowUp = params.has("followup");
  const source = useRun(sourceId ?? "");
  if (!sourceId) return <ResearchForm />;
  if (!source.data) return <p role="status">Loading the earlier run...</p>;
  const r = source.data;
  return (
    <div className="flex flex-col gap-3">
      <Alert kind="info">
        {isFollowUp
          ? "Follow-up: this starts a new run in the same conversation, with a new estimate."
          : "Re-run: the earlier settings are prefilled; a new estimate is required."}
      </Alert>
      <ResearchForm
        key={sourceId}
        followUp={{
          conversationId: isFollowUp ? (r.conversation_id ?? "") : "",
          instrumentId: r.instrument_id,
          horizon: r.horizon,
          profileId: r.model_profile_id,
        }}
      />
    </div>
  );
}
