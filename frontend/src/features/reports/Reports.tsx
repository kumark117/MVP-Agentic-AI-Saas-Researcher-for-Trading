"use client";

/**
 * Personal reports (R044-R048; D106): counts, success rate with numerator and
 * denominator, latency populations with sample counts and low-sample warnings, usage,
 * real provider cost vs simulated charge, model comparison and a daily cost trend.
 * Shows data_as_of, the filters applied and each metric's window basis.
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Alert, Card, DemoMoney, ErrorText, RealCost, Timestamp } from "@/components/ui";
import { api, call, type Schemas } from "@/lib/api/client";

function ms(v: number | null | undefined): string {
  return v === null || v === undefined
    ? "none"
    : v < 1000
      ? `${Math.round(v)} ms`
      : `${(v / 1000).toFixed(1)} s`;
}

function DistRow({ name, d }: { name: string; d: Schemas["LatencyDistribution"] }) {
  return (
    <tr className="border-t border-gray-200 dark:border-gray-800">
      <th scope="row" className="font-normal">
        {name}
      </th>
      <td className="tabular-nums">
        {d.sample_count}
        {d.low_sample && d.sample_count > 0 ? " (low sample)" : ""}
      </td>
      <td className="tabular-nums">{ms(d.p50_ms)}</td>
      <td className="tabular-nums">{ms(d.p95_ms)}</td>
      <td className="tabular-nums">{ms(d.p99_ms)}</td>
      <td className="text-xs">{d.population}</td>
    </tr>
  );
}

export function Reports() {
  const [days, setDays] = useState(30);
  const [range] = useState(() => {
    const to = new Date();
    return { to: to.toISOString() };
  });
  const from = new Date(Date.parse(range.to) - days * 86400_000).toISOString();
  const q = { from, to: range.to };
  const summary = useQuery({
    queryKey: ["reports", "summary", q],
    queryFn: () =>
      call<Schemas["Summary"]>(() => api.GET("/api/v1/reports/summary", { params: { query: q } })),
  });
  const models = useQuery({
    queryKey: ["reports", "models", q],
    queryFn: () =>
      call<Schemas["ModelsReport"]>(() =>
        api.GET("/api/v1/reports/models", { params: { query: q } }),
      ),
  });
  const s = summary.data;
  const maxCost = Math.max(0.0001, ...(s?.daily.map((d) => Number(d.provider_cost_usd)) ?? [0]));

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-semibold">Reports</h1>
      <label className="flex items-center gap-2 text-sm">
        Window
        <select
          className="rounded border border-gray-400 bg-transparent px-3 py-2"
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
        >
          <option value={7}>Last 7 days</option>
          <option value={30}>Last 30 days</option>
          <option value={90}>Last 90 days</option>
        </select>
      </label>
      <ErrorText error={summary.error ?? models.error} />
      {s && (
        <>
          <p className="text-sm">
            Data as of <Timestamp iso={s.data_as_of} /> (UTC). Windows: submissions by{" "}
            {s.window_basis.counts}, outcomes by {s.window_basis.outcomes}, money by{" "}
            {s.window_basis.financial}, so totals can differ between sections.
          </p>
          <div className="grid gap-6 lg:grid-cols-2">
            <Card title="Runs">
              <dl className="grid grid-cols-2 gap-1 text-sm">
                {Object.entries(s.counts).map(([k, v]) => (
                  <div key={k} className="contents">
                    <dt>{k.replaceAll("_", " ")}</dt>
                    <dd className="tabular-nums">{v}</dd>
                  </div>
                ))}
                <dt className="font-semibold">Success rate</dt>
                <dd>
                  {s.success_rate.value === null
                    ? "no finished runs"
                    : `${(s.success_rate.value * 100).toFixed(1)}%`}{" "}
                  ({s.success_rate.numerator} of {s.success_rate.denominator})
                </dd>
              </dl>
              <p className="mt-2 text-xs">
                {s.success_rate.definition}. Cancellations and queue expiries are shown separately.
              </p>
            </Card>
            <Card title="Cost and usage">
              <dl className="grid grid-cols-2 gap-1 text-sm">
                <dt>Actual provider cost</dt>
                <dd>
                  <RealCost value={s.provider_cost.amount} status={s.provider_cost.cost_status} />
                </dd>
                <dt>Simulated charges</dt>
                <dd>
                  <DemoMoney value={s.customer_charge} />
                </dd>
                <dt>Money collected</dt>
                <dd>₹0.00</dd>
                <dt>Input tokens</dt>
                <dd className="tabular-nums">{s.usage.input_tokens}</dd>
                <dt>Output tokens</dt>
                <dd className="tabular-nums">{s.usage.output_tokens}</dd>
                <dt>Cached input tokens</dt>
                <dd className="tabular-nums">{s.usage.cached_input_tokens}</dd>
                <dt>Unknown-usage calls</dt>
                <dd className="tabular-nums">{s.usage.unknown_call_count}</dd>
                <dt>Retries / tool errors</dt>
                <dd className="tabular-nums">
                  {s.retries} / {s.tool_errors}
                </dd>
                <dt>Validation failures / security blocks</dt>
                <dd className="tabular-nums">
                  {s.validation_failures} / {s.security_blocks}
                </dd>
              </dl>
              <p className="mt-2 text-xs">{s.usage.note}</p>
            </Card>
          </div>
          <Card title="Latency">
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <caption className="sr-only">Latency percentiles by population</caption>
                <thead>
                  <tr>
                    <th scope="col">Metric</th>
                    <th scope="col">Samples</th>
                    <th scope="col">p50</th>
                    <th scope="col">p95</th>
                    <th scope="col">p99</th>
                    <th scope="col">Population</th>
                  </tr>
                </thead>
                <tbody>
                  <DistRow name="Successful runs, end to end" d={s.latency.success_end_to_end} />
                  <DistRow
                    name="All finished runs, end to end"
                    d={s.latency.all_terminal_end_to_end}
                  />
                  <DistRow name="Queue time" d={s.latency.queue} />
                  <DistRow name="Execution time" d={s.latency.execution} />
                </tbody>
              </table>
            </div>
            <p className="mt-2 text-xs">
              Exact percentiles over raw durations (not averages of daily values).
            </p>
          </Card>
          <Card title="Daily real cost (USD)">
            <ul className="flex flex-col gap-1 text-xs" aria-label="Daily real provider cost">
              {s.daily
                .filter((d) => d.submitted > 0 || Number(d.provider_cost_usd) > 0)
                .map((d) => (
                  <li key={d.day} className="flex items-center gap-2">
                    <span className="w-24 tabular-nums">{d.day}</span>
                    {/* SVG attributes, not inline styles: the CSP forbids style attributes. */}
                    <svg aria-hidden="true" width="240" height="12">
                      <rect
                        width={Math.max(2, (Number(d.provider_cost_usd) / maxCost) * 240)}
                        height="12"
                        className="fill-blue-700"
                      />
                    </svg>
                    <span className="tabular-nums">
                      ${d.provider_cost_usd} real · {d.submitted} runs · ₹{d.customer_charge_inr}{" "}
                      simulated
                    </span>
                  </li>
                ))}
            </ul>
          </Card>
          {s.warnings.map((w) => (
            <Alert key={w} kind="warning">
              {w}
            </Alert>
          ))}
        </>
      )}
      {models.data && (
        <Card title="Model comparison">
          {models.data.models.length === 0 && (
            <p className="text-sm">No model calls in this window.</p>
          )}
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Per-model usage, cost, success and latency</caption>
              <thead>
                <tr>
                  <th scope="col">Model</th>
                  <th scope="col">Calls</th>
                  <th scope="col">Tokens in/out</th>
                  <th scope="col">Real cost</th>
                  <th scope="col">Simulated</th>
                  <th scope="col">Success</th>
                  <th scope="col">Call p50 / p95</th>
                </tr>
              </thead>
              <tbody>
                {models.data.models.map((m) => (
                  <tr key={m.model_id} className="border-t border-gray-200 dark:border-gray-800">
                    <td>{m.model_id}</td>
                    <td className="tabular-nums">{m.calls}</td>
                    <td className="tabular-nums">
                      {m.usage.input_tokens} / {m.usage.output_tokens}
                    </td>
                    <td>
                      <RealCost
                        value={m.provider_cost.amount}
                        status={m.provider_cost.cost_status}
                      />
                    </td>
                    <td>
                      <DemoMoney value={m.customer_charge} />
                    </td>
                    <td>
                      {m.success_rate.value === null
                        ? "none"
                        : `${m.success_rate.numerator}/${m.success_rate.denominator}`}
                    </td>
                    <td className="tabular-nums">
                      {ms(m.latency.p50_ms)} / {ms(m.latency.p95_ms)}
                      {m.latency.low_sample ? " (low sample)" : ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {models.data.warnings.map((w) => (
            <p key={w} className="mt-1 text-xs">
              {w}
            </p>
          ))}
        </Card>
      )}
    </div>
  );
}
