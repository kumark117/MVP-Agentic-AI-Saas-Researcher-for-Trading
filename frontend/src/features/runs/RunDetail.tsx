"use client";

/**
 * Run detail (R024-R031; D020-D024): RunStatus, ActivityPanel, EvidencePanel, ReportPanel
 * and UsagePanel. Live updates come from the run stream; the persisted snapshot stays
 * authoritative and is refetched on report.ready, billing.updated and run.finished.
 * Closing this page or the stream is not cancellation (D050).
 */
import { useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { useAnnounce } from "@/components/Announcer";
import {
  Alert,
  Button,
  Card,
  DemoMoney,
  Duration,
  ErrorText,
  RealCost,
  SafeText,
  Timestamp,
} from "@/components/ui";
import { api, ApiError, call, type Schemas } from "@/lib/api/client";
import { keys, useActivity, useReport, useRun, useUsage } from "@/lib/api/queries";
import { RUN_EVENT_TYPES, StreamController, type StreamEvent } from "@/lib/sse/StreamController";

const TERMINAL = new Set(["succeeded", "incomplete", "failed", "cancelled", "timed_out"]);
const BRANCHES = ["price_branch", "news_branch", "technical_branch"];

type LiveItem = { id: string; seq: number; at: string; text: string; kind: string };

function describe(e: StreamEvent): string | null {
  const p = e.payload as Record<string, unknown>;
  switch (e.type) {
    case "activity.appended":
      return `${p.agent ? `${String(p.agent)}: ` : ""}${String(p.summary ?? "")}`;
    case "tool.completed":
      return `Tool ${String(p.tool_name)}: ${String(p.observation_summary)} (${String(p.validation_status)})`;
    case "tool.failed":
      return `Tool ${String(p.tool_name)} failed: ${String(p.error_code)}`;
    case "validation.completed":
      return `Validation (${String(p.boundary)}): ${String(p.verdict)}`;
    case "run.cancel_requested":
      return "Cancellation requested.";
    case "report.ready":
      return `Report ready: ${String(p.signal)}`;
    case "run.finished":
      return `Run finished: ${String(p.status)}`;
    default:
      return null;
  }
}

export function RunDetail() {
  const { runId } = useParams<{ runId: string }>();
  const qc = useQueryClient();
  const router = useRouter();
  const announce = useAnnounce();
  const run = useRun(runId);
  const activity = useActivity(runId);
  const usage = useUsage(runId);
  const terminal = !!run.data && TERMINAL.has(run.data.status);
  const report = useReport(runId, !!run.data?.report_available);
  const [live, setLive] = useState<LiveItem[]>([]);
  const [completed, setCompleted] = useState<Set<string>>(new Set());
  const [streamState, setStreamState] = useState("connecting");
  const startedFrom = useRef<string | null | undefined>(undefined);

  useEffect(() => {
    if (!run.data || startedFrom.current !== undefined) return;
    startedFrom.current = null; // replay everything retained, then go live
    const c = new StreamController({
      url: `/api/v1/research/runs/${runId}/events`,
      eventTypes: RUN_EVENT_TYPES,
      closeOn: ["run.finished"],
      onStatus: setStreamState,
      probe: async () => {
        try {
          const snap = await call<Schemas["RunSnapshot"]>(() =>
            api.GET("/api/v1/research/runs/{run_id}", { params: { path: { run_id: runId } } }),
          );
          void qc.invalidateQueries({ queryKey: keys.run(runId) });
          return { kind: "ok", latestEventId: snap.latest_event_id };
        } catch (err) {
          return err instanceof ApiError && err.status === 401
            ? { kind: "unauthenticated" }
            : { kind: "gone" };
        }
      },
      onEvent: (e) => {
        const text = describe(e);
        if (text)
          setLive((l) => [
            ...l,
            { id: e.id, seq: e.sequence, at: e.occurredAt, text, kind: e.type },
          ]);
        if (e.type === "node.started" || e.type === "activity.appended") {
          void qc.invalidateQueries({ queryKey: keys.run(runId) });
        }
        if (
          e.type === "activity.appended" &&
          (e.payload as { action?: string }).action === "branch_finished"
        ) {
          const agent = String((e.payload as { agent?: string }).agent);
          setCompleted((s) => {
            const n = new Set(s).add(`${agent}_branch`);
            announce(`${agent} branch complete, ${n.size} of ${BRANCHES.length}`);
            return n;
          });
        }
        if (["report.ready", "billing.updated", "run.finished"].includes(e.type)) {
          void qc.invalidateQueries({ queryKey: keys.run(runId) });
          void qc.invalidateQueries({ queryKey: keys.usage(runId) });
          void qc.invalidateQueries({ queryKey: keys.report(runId) });
          void qc.invalidateQueries({ queryKey: keys.balance });
        }
        if (e.type === "run.finished") announce(`Research finished: ${String(e.payload.status)}`);
      },
    });
    c.start();
    return () => c.stop();
  }, [run.data, runId, qc, announce]);

  useEffect(() => {
    if (run.error instanceof ApiError && run.error.status === 401) router.replace("/login");
  }, [run.error, router]);

  if (run.error) return <ErrorText error={run.error} />;
  if (!run.data) return <p role="status">Loading run...</p>;
  const r = run.data;

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-semibold">
        {String(r.instrument_snapshot.display_name ?? "Research run")} ·{" "}
        {r.horizon.replace("_", " ")}
      </h1>
      <RunStatus run={r} streamState={streamState} completed={completed.size} />
      <div className="grid gap-6 lg:grid-cols-2">
        <ActivityPanel persisted={activity.data?.items ?? []} live={live} />
        <EvidencePanel report={report.data} />
      </div>
      <ReportPanel run={r} report={report.data} error={report.error} terminal={terminal} />
      <UsagePanel usage={usage.data} />
    </div>
  );
}

function RunStatus({
  run,
  streamState,
  completed,
}: {
  run: Schemas["RunSnapshot"];
  streamState: string;
  completed: number;
}) {
  const qc = useQueryClient();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const cancellable = ["queued", "running"].includes(run.status);

  async function cancel() {
    setPending(true);
    setError(null);
    try {
      await call(() =>
        api.POST("/api/v1/research/runs/{run_id}/cancel", {
          params: { path: { run_id: run.run_id } },
          body: {},
        }),
      );
      void qc.invalidateQueries({ queryKey: keys.run(run.run_id) });
    } catch (e) {
      setError(e);
    } finally {
      setPending(false);
    }
  }

  return (
    <Card title="Status">
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-4">
        <dt>State</dt>
        <dd className="font-semibold">{run.status.replace("_", " ")}</dd>
        <dt>Billing</dt>
        <dd>{run.billing_status.replace("_", " ")}</dd>
        <dt>Submitted</dt>
        <dd>
          <Timestamp iso={run.submitted_at} />
        </dd>
        <dt>Queue time</dt>
        <dd>
          <Duration ms={run.queue_ms} />
        </dd>
        <dt>Execution</dt>
        <dd>
          <Duration ms={run.execution_ms} />
        </dd>
        <dt>First activity</dt>
        <dd>
          <Duration ms={run.time_to_first_activity_ms} />
        </dd>
        <dt>Progress</dt>
        <dd>
          {run.progress.completed_nodes} of {run.progress.total_nodes} steps ({completed} of 3
          branches)
        </dd>
        <dt>Live updates</dt>
        <dd>{streamState}</dd>
        <dt>Reserved</dt>
        <dd>
          <DemoMoney value={run.reserved_amount} />
        </dd>
        <dt>Charged</dt>
        <dd>
          <DemoMoney value={run.settled_charge} />
        </dd>
      </dl>
      {run.outcome_reason && (
        <p className="mt-2 text-sm">Outcome: {run.outcome_reason.replaceAll("_", " ")}</p>
      )}
      {run.observability_gap && (
        <p className="mt-1 text-xs">Trace export unavailable for this run (recorded gap).</p>
      )}
      {cancellable && (
        <div className="mt-3 flex flex-col gap-2">
          <Button variant="danger" onClick={() => void cancel()} disabled={pending}>
            {pending ? "Cancelling..." : "Cancel research"}
          </Button>
          <p className="text-xs">
            Provider work already started may still incur real platform cost.
          </p>
        </div>
      )}
      {run.status === "cancel_requested" && <Alert kind="info">Cancellation is in progress.</Alert>}
      <ErrorText error={error} />
    </Card>
  );
}

function ActivityPanel({
  persisted,
  live,
}: {
  persisted: Schemas["Activity"][];
  live: LiveItem[];
}) {
  const [paused, setPaused] = useState(false);
  const end = useRef<HTMLLIElement | null>(null);
  const items = useMemo(() => {
    const seen = new Set<string>();
    const out: LiveItem[] = [];
    for (const a of persisted) {
      seen.add(a.event_id);
      out.push({
        id: a.event_id,
        seq: a.sequence,
        at: a.occurred_at,
        kind: "activity.appended",
        text: `${a.agent ? `${a.agent}: ` : ""}${a.summary}`,
      });
    }
    for (const l of live) if (!seen.has(l.id)) out.push(l);
    return out.sort((a, b) => a.seq - b.seq);
  }, [persisted, live]);

  useEffect(() => {
    if (!paused) end.current?.scrollIntoView({ block: "nearest", behavior: "auto" });
  }, [items.length, paused]);

  function download() {
    const blob = new Blob([items.map((i) => `${i.at}\t${i.text}`).join("\n")], {
      type: "text/plain",
    });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "activity.txt";
    a.click();
    URL.revokeObjectURL(a.href);
  }

  return (
    <Card title="Activity">
      <div className="mb-2 flex gap-2">
        <Button variant="secondary" onClick={() => setPaused((p) => !p)} aria-pressed={paused}>
          {paused ? "Resume auto-scroll" : "Pause auto-scroll"}
        </Button>
        <Button variant="secondary" onClick={download}>
          Download
        </Button>
      </div>
      <ol
        className="max-h-80 overflow-y-auto text-sm motion-safe:scroll-smooth"
        tabIndex={0}
        aria-label="Activity log"
      >
        {items.map((i) => (
          <li key={i.id} className="border-b border-gray-200 py-1 dark:border-gray-800">
            <span className="mr-2 text-xs opacity-75">
              <Timestamp iso={i.at} />
            </span>
            {i.text}
          </li>
        ))}
        <li ref={end} aria-hidden="true" />
      </ol>
      <p className="mt-2 text-xs">
        Actions and observations only; model reasoning is never shown or stored.
      </p>
    </Card>
  );
}

function EvidencePanel({ report }: { report: Schemas["Report"] | undefined }) {
  return (
    <Card title="Evidence">
      {!report && <p className="text-sm">Cited sources appear when the report is ready.</p>}
      {report && (
        <ul className="flex flex-col gap-2 text-sm">
          {report.citations.map((c) => (
            <li key={c.evidence_id}>
              <span className="font-medium">{c.source_title}</span> · observed{" "}
              <Timestamp iso={c.observed_at} />
              {c.source_url && (
                <>
                  {" "}
                  ·{" "}
                  <a
                    href={c.source_url}
                    target="_blank"
                    rel="noopener noreferrer nofollow"
                    className="underline"
                  >
                    source
                  </a>
                </>
              )}
            </li>
          ))}
          <li className="text-xs">Freshness: {report.freshness_state.replace("_", " ")}</li>
          {report.limitations
            .filter((l) => l.includes("quarantined"))
            .map((l) => (
              <li key={l}>
                <Alert kind="warning">{l}</Alert>
              </li>
            ))}
        </ul>
      )}
    </Card>
  );
}

function ReportPanel({
  run,
  report,
  error,
  terminal,
}: {
  run: Schemas["RunSnapshot"];
  report: Schemas["Report"] | undefined;
  error: unknown;
  terminal: boolean;
}) {
  if (!run.report_available) {
    return (
      <Card title="Report">
        <p className="text-sm">
          {terminal ? "No report was produced for this run." : "The report is being prepared."}
        </p>
      </Card>
    );
  }
  if (!report)
    return (
      <Card title="Report">{error ? <ErrorText error={error} /> : <p>Loading report...</p>}</Card>
    );
  return (
    <Card title="Report">
      <p className="text-xl font-semibold">
        Signal: {report.signal}
        {report.signal === "NO_SIGNAL" && (
          <span className="ml-2 text-sm font-normal">
            (the evidence does not support a reliable call)
          </span>
        )}
      </p>
      <p className="text-sm">
        Confidence: {report.confidence.label ?? "none"}
        {report.confidence.score !== null && report.confidence.score !== undefined
          ? ` (${report.confidence.score.toFixed(2)})`
          : ""}{" "}
        · uncalibrated, not a probability
      </p>
      <div className="mt-3">
        <SafeText text={report.summary} />
      </div>
      {report.missing_evidence.length > 0 && (
        <Alert kind="warning">Missing evidence: {report.missing_evidence.join(" ")}</Alert>
      )}
      <div className="mt-3 grid gap-4 md:grid-cols-2">
        <section aria-label="Supporting evidence">
          <h3 className="font-semibold">Supporting</h3>
          <ul className="list-disc pl-5 text-sm">
            {report.supporting_evidence.map((c, i) => (
              <li key={i}>{c.claim}</li>
            ))}
          </ul>
        </section>
        <section aria-label="Opposing evidence">
          <h3 className="font-semibold">Opposing</h3>
          <ul className="list-disc pl-5 text-sm">
            {report.opposing_evidence.map((c, i) => (
              <li key={i}>{c.claim}</li>
            ))}
          </ul>
        </section>
      </div>
      {(["assumptions", "risks", "limitations"] as const).map((k) => (
        <section key={k} aria-label={k} className="mt-3">
          <h3 className="font-semibold capitalize">{k}</h3>
          <ul className="list-disc pl-5 text-sm">
            {report[k].map((t, i) => (
              <li key={i}>{t}</li>
            ))}
          </ul>
        </section>
      ))}
      <p className="mt-3 text-xs">
        Data as of <Timestamp iso={report.data_as_of} />. {report.research_only_disclosure}
      </p>
      <p className="text-xs">
        Validation: {report.validation_summary.passed ? "passed" : "did not pass"}
        {report.validation_summary.repaired ? " after one repair" : ""}.
      </p>
    </Card>
  );
}

function UsagePanel({ usage }: { usage: Schemas["RunUsage"] | undefined }) {
  if (!usage) return null;
  return (
    <Card title="Usage and cost">
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-4">
        <dt>Actual provider cost</dt>
        <dd>
          <RealCost value={usage.provider_cost_total} status={usage.provider_cost_status} />
        </dd>
        <dt>Simulated charge</dt>
        <dd>
          <DemoMoney value={usage.settled_charge} />
        </dd>
        <dt>Released</dt>
        <dd>
          <DemoMoney value={usage.released_amount} />
        </dd>
        <dt>Pending</dt>
        <dd>
          <DemoMoney value={usage.pending_amount} />
        </dd>
        <dt>Money collected</dt>
        <dd>₹0.00</dd>
        <dt>Rate version</dt>
        <dd>{usage.price_version}</dd>
        <dt>Unknown usage calls</dt>
        <dd>{usage.unknown_call_count}</dd>
      </dl>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full text-left text-xs">
          <caption className="sr-only">Model calls</caption>
          <thead>
            <tr>
              <th scope="col">Step</th>
              <th scope="col">Model</th>
              <th scope="col">Tokens in/out</th>
              <th scope="col">Usage</th>
              <th scope="col">Cost (real)</th>
              <th scope="col">Charge (simulated)</th>
            </tr>
          </thead>
          <tbody>
            {usage.calls.map((c) => (
              <tr key={c.call_id} className="border-t border-gray-200 dark:border-gray-800">
                <td>
                  {c.node_id} ({c.purpose}
                  {c.billable ? "" : ", not billed"})
                </td>
                <td>
                  {c.model_id ?? c.model_requested}
                  {c.fallback_used ? " (fallback)" : ""}
                </td>
                <td className="tabular-nums">
                  {c.token_categories.input_tokens ?? "unknown"} /{" "}
                  {c.token_categories.output_tokens ?? "unknown"}
                </td>
                <td>{c.usage_status}</td>
                <td>
                  <RealCost value={c.provider_cost} status={c.cost_status} />
                </td>
                <td>
                  <DemoMoney value={c.computed_charge} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
