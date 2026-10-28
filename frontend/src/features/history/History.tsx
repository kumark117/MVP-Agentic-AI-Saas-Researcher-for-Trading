"use client";

/**
 * History (R033-R035): filters (date, instrument, model, state), cursor paging, reopen,
 * re-run (always a new estimate, never a silent repeat) and asynchronous exports.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRef, useState } from "react";

import { Alert, Button, Card, DemoMoney, Duration, ErrorText, Timestamp } from "@/components/ui";
import { api, call, newIdempotencyKey, type Schemas } from "@/lib/api/client";
import { useInstruments, useModels } from "@/lib/api/queries";

type Filters = {
  status?: string;
  instrument_id?: string;
  model_profile_id?: string;
  from?: string;
  to?: string;
};
const STATES = [
  "queued",
  "running",
  "cancel_requested",
  "succeeded",
  "incomplete",
  "failed",
  "cancelled",
  "timed_out",
];

function iso(date: string | undefined, endOfDay = false): string | undefined {
  if (!date) return undefined;
  return new Date(`${date}T${endOfDay ? "23:59:59.999" : "00:00:00"}Z`).toISOString();
}

export function History() {
  const instruments = useInstruments();
  const models = useModels();
  const [filters, setFilters] = useState<Filters>({});
  const [cursors, setCursors] = useState<string[]>([]);
  const cursor = cursors.at(-1);
  const query = {
    limit: 20,
    cursor,
    status: filters.status || undefined,
    instrument_id: filters.instrument_id || undefined,
    model_profile_id: filters.model_profile_id || undefined,
    from: iso(filters.from),
    to: iso(filters.to, true),
  };
  const runs = useQuery({
    queryKey: ["runs", "history", query],
    queryFn: () =>
      call<Schemas["Page_RunSummary_"]>(() =>
        api.GET("/api/v1/research/runs", { params: { query: query as never } }),
      ),
  });
  const set = (k: keyof Filters, v: string) => {
    setFilters((f) => ({ ...f, [k]: v }));
    setCursors([]);
  };
  const select = "rounded border border-gray-400 bg-transparent px-3 py-2";

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-semibold">History</h1>
      <Card title="Filters">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <label className="flex flex-col text-sm">
            From{" "}
            <input
              type="date"
              className={select}
              value={filters.from ?? ""}
              onChange={(e) => set("from", e.target.value)}
            />
          </label>
          <label className="flex flex-col text-sm">
            To{" "}
            <input
              type="date"
              className={select}
              value={filters.to ?? ""}
              onChange={(e) => set("to", e.target.value)}
            />
          </label>
          <label className="flex flex-col text-sm">
            Instrument
            <select
              className={select}
              value={filters.instrument_id ?? ""}
              onChange={(e) => set("instrument_id", e.target.value)}
            >
              <option value="">All</option>
              {instruments.data?.instruments.map((i) => (
                <option key={i.instrument_id} value={i.instrument_id}>
                  {i.display_name}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col text-sm">
            Model profile
            <select
              className={select}
              value={filters.model_profile_id ?? ""}
              onChange={(e) => set("model_profile_id", e.target.value)}
            >
              <option value="">All</option>
              {models.data?.map((m) => (
                <option key={m.model_profile_id} value={m.model_profile_id}>
                  {m.display_name}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col text-sm">
            State
            <select
              className={select}
              value={filters.status ?? ""}
              onChange={(e) => set("status", e.target.value)}
            >
              <option value="">All</option>
              {STATES.map((s) => (
                <option key={s} value={s}>
                  {s.replace("_", " ")}
                </option>
              ))}
            </select>
          </label>
        </div>
      </Card>

      <Card title="Runs">
        <ErrorText error={runs.error} />
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Research runs matching the filters</caption>
            <thead>
              <tr>
                <th scope="col">Submitted</th>
                <th scope="col">Instrument</th>
                <th scope="col">State</th>
                <th scope="col">Signal</th>
                <th scope="col">Duration</th>
                <th scope="col">Charge</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {runs.data?.items.map((r) => (
                <tr key={r.run_id} className="border-t border-gray-200 dark:border-gray-800">
                  <td>
                    <Timestamp iso={r.submitted_at} />
                  </td>
                  <td>
                    {r.instrument_display_name} · {r.horizon.replace("_", " ")}
                  </td>
                  <td>{r.status.replace("_", " ")}</td>
                  <td>{r.signal ?? "none"}</td>
                  <td>
                    <Duration ms={r.end_to_end_ms} />
                  </td>
                  <td>
                    <DemoMoney value={r.settled_charge} />
                  </td>
                  <td className="flex gap-2">
                    <Link className="underline" href={`/runs/${r.run_id}`}>
                      Open
                    </Link>
                    <Link className="underline" href={`/research?rerun=${r.run_id}`}>
                      Re-run
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {runs.data?.items.length === 0 && <p className="text-sm">No runs match these filters.</p>}
        <div className="mt-3 flex gap-2">
          {cursors.length > 0 && (
            <Button variant="secondary" onClick={() => setCursors((c) => c.slice(0, -1))}>
              Newer
            </Button>
          )}
          {runs.data?.next_cursor && (
            <Button
              variant="secondary"
              onClick={() => setCursors((c) => [...c, runs.data!.next_cursor!])}
            >
              Older
            </Button>
          )}
        </div>
      </Card>
      <Exports />
    </div>
  );
}

function Exports() {
  const qc = useQueryClient();
  const [kind, setKind] = useState<"runs" | "usage" | "account_data">("runs");
  const [format, setFormat] = useState<"csv" | "json">("csv");
  const [from, setFrom] = useState(() =>
    new Date(Date.now() - 30 * 86400_000).toISOString().slice(0, 10),
  );
  const [to, setTo] = useState(() => new Date().toISOString().slice(0, 10));
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const key = useRef<{ sig: string; key: string } | null>(null);
  const job = useQuery({
    queryKey: ["export", jobId],
    enabled: !!jobId,
    queryFn: () =>
      call<Schemas["ExportJob"]>(() =>
        api.GET("/api/v1/exports/{export_id}", { params: { path: { export_id: jobId! } } }),
      ),
    refetchInterval: (q) =>
      q.state.data && ["ready", "failed", "expired"].includes(q.state.data.status) ? false : 1500,
  });

  async function start() {
    const sig = `${kind}|${format}|${from}|${to}`;
    if (key.current?.sig !== sig) key.current = { sig, key: newIdempotencyKey() };
    setError(null);
    try {
      const j = await call<Schemas["ExportJob"]>(() =>
        api.POST("/api/v1/exports", {
          body: { kind, format, from: iso(from)!, to: iso(to, true)! },
          params: { header: { "Idempotency-Key": key.current!.key } },
        }),
      );
      setJobId(j.export_id);
      void qc.invalidateQueries({ queryKey: ["export", j.export_id] });
    } catch (e) {
      setError(e);
    }
  }

  const select = "rounded border border-gray-400 bg-transparent px-3 py-2";
  return (
    <Card title="Export your data">
      <div className="grid gap-3 sm:grid-cols-4">
        <label className="flex flex-col text-sm">
          What
          <select
            className={select}
            value={kind}
            onChange={(e) => setKind(e.target.value as typeof kind)}
          >
            <option value="runs">Runs</option>
            <option value="usage">Usage</option>
            <option value="account_data">Account data</option>
          </select>
        </label>
        <label className="flex flex-col text-sm">
          Format
          <select
            className={select}
            value={format}
            onChange={(e) => setFormat(e.target.value as typeof format)}
          >
            <option value="csv">CSV</option>
            <option value="json">JSON</option>
          </select>
        </label>
        <label className="flex flex-col text-sm">
          From{" "}
          <input
            type="date"
            className={select}
            value={from}
            onChange={(e) => setFrom(e.target.value)}
          />
        </label>
        <label className="flex flex-col text-sm">
          To{" "}
          <input
            type="date"
            className={select}
            value={to}
            onChange={(e) => setTo(e.target.value)}
          />
        </label>
      </div>
      <div className="mt-3">
        <Button onClick={() => void start()}>Create export</Button>
      </div>
      <ErrorText error={error} />
      {job.data && (
        <div className="mt-3 text-sm">
          {job.data.status === "ready" && job.data.download_url ? (
            <Alert kind="success">
              Export ready ({job.data.row_count} rows).{" "}
              <a className="underline" href={job.data.download_url}>
                Download
              </a>{" "}
              (link expires <Timestamp iso={job.data.download_expires_at} />)
            </Alert>
          ) : (
            <p role="status">Export {job.data.status}...</p>
          )}
        </div>
      )}
    </Card>
  );
}
