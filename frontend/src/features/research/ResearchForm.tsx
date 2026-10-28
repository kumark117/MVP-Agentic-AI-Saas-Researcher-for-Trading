"use client";

/**
 * Research form and estimate card (R028-R031; D018-D020).
 * metal -> venue (dependent) -> instrument -> horizon (supported only) -> model profile,
 * optional question (<= 4,000 characters). Editing any field invalidates the estimate.
 * Start research is disabled while pending and reuses one Idempotency-Key across retries
 * of the same intent, so a double click or a lost response never starts two runs.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { Alert, Button, Card, DemoMoney, ErrorText, RealCost } from "@/components/ui";
import { api, call, newIdempotencyKey, type Schemas } from "@/lib/api/client";
import { keys, useBalance, useInstruments, useModels } from "@/lib/api/queries";

type Estimate = Schemas["Estimate"];
const MAX_Q = 4000;

function useCountdown(until: string | undefined): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!until) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [until]);
  return until ? Math.max(0, Math.floor((Date.parse(until) - now) / 1000)) : 0;
}

export function ResearchForm({
  followUp,
}: {
  followUp?: { conversationId: string; instrumentId: string; horizon: string; profileId: string };
}) {
  const router = useRouter();
  const qc = useQueryClient();
  const catalog = useInstruments();
  const models = useModels();
  const balance = useBalance();

  const [metalChoice, setMetal] = useState("");
  const [venueChoice, setVenue] = useState("");
  const [instrumentId, setInstrumentId] = useState(followUp?.instrumentId ?? "");
  const [horizon, setHorizon] = useState(followUp?.horizon ?? "");
  const [profileChoice, setProfileId] = useState(followUp?.profileId ?? "");
  const [question, setQuestion] = useState("");
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const [startKey, setStartKey] = useState<string | null>(null);

  const all = useMemo(() => catalog.data?.instruments ?? [], [catalog.data]);
  // Derived defaults: a follow-up preselects its instrument; the default profile is used
  // until the member picks one.
  const preselected = all.find((i) => i.instrument_id === followUp?.instrumentId);
  const metal = metalChoice || preselected?.metal || "";
  const venue = venueChoice || preselected?.venue || "";
  const profileId =
    profileChoice ||
    (models.data?.find((m) => m.is_default) ?? models.data?.[0])?.model_profile_id ||
    "";
  const metals = useMemo(() => [...new Set(all.map((i) => i.metal))].sort(), [all]);
  const venues = useMemo(
    () => [...new Set(all.filter((i) => i.metal === metal).map((i) => i.venue))].sort(),
    [all, metal],
  );
  const instruments = all.filter((i) => i.metal === metal && i.venue === venue);
  const selected = all.find((i) => i.instrument_id === instrumentId);
  const unavailable = (catalog.data?.unavailable ?? []).filter((u) => !metal || u.metal === metal);

  const secondsLeft = useCountdown(estimate?.expires_at);
  const expired = !!estimate && secondsLeft === 0;

  function invalidate() {
    setEstimate(null);
    setStartKey(null);
    setError(null);
  }

  async function getEstimate() {
    setPending(true);
    setError(null);
    try {
      const est = await call<Estimate>(() =>
        api.POST("/api/v1/research/estimates", {
          body: {
            instrument_id: instrumentId,
            horizon: horizon as "intraday" | "swing" | "long_term",
            model_profile_id: profileId,
            question: question.trim() || null,
            conversation_id: followUp?.conversationId ?? null,
          },
        }),
      );
      setEstimate(est);
      setStartKey(newIdempotencyKey());
    } catch (e) {
      setError(e);
    } finally {
      setPending(false);
    }
  }

  async function start() {
    if (!estimate || !startKey) return;
    setPending(true);
    setError(null);
    try {
      const run = await call<Schemas["RunAccepted"]>(() =>
        api.POST("/api/v1/research/runs", {
          body: { estimate_id: estimate.estimate_id, max_charge: estimate.max_charge },
          params: { header: { "Idempotency-Key": startKey } },
        }),
      );
      void qc.invalidateQueries({ queryKey: keys.balance });
      void qc.invalidateQueries({ queryKey: ["runs"] });
      router.push(`/runs/${run.run_id}`);
    } catch (e) {
      setError(e);
      setPending(false);
    }
  }

  const profile = models.data?.find((m) => m.model_profile_id === profileId);
  const canEstimate = !!instrumentId && !!horizon && !!profileId && question.length <= MAX_Q;
  const select = "rounded border border-gray-400 bg-transparent px-3 py-2";

  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card title="New research">
        <form
          className="flex flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            void getEstimate();
          }}
        >
          <div className="flex flex-col gap-1">
            <label htmlFor="metal" className="text-sm font-medium">
              Metal
            </label>
            <select
              id="metal"
              className={select}
              value={metal}
              onChange={(e) => {
                setMetal(e.target.value);
                setVenue("");
                setInstrumentId("");
                setHorizon("");
                invalidate();
              }}
            >
              <option value="">Choose a metal</option>
              {metals.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor="venue" className="text-sm font-medium">
              Venue
            </label>
            <select
              id="venue"
              className={select}
              value={venue}
              disabled={!metal}
              onChange={(e) => {
                setVenue(e.target.value);
                setInstrumentId("");
                setHorizon("");
                invalidate();
              }}
            >
              <option value="">Choose a venue</option>
              {venues.map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
          </div>
          <fieldset className="flex flex-col gap-2" disabled={!venue}>
            <legend className="text-sm font-medium">Instrument</legend>
            {instruments.length === 0 && (
              <p className="text-sm opacity-75">Choose a metal and venue.</p>
            )}
            {instruments.map((i) => (
              <label
                key={i.instrument_id}
                className={`flex flex-col rounded border p-2 ${i.enabled ? "" : "opacity-60"}`}
              >
                <span className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="instrument"
                    value={i.instrument_id}
                    disabled={!i.enabled}
                    checked={instrumentId === i.instrument_id}
                    onChange={() => {
                      setInstrumentId(i.instrument_id);
                      setHorizon("");
                      invalidate();
                    }}
                  />
                  <span className="font-medium">{i.display_name}</span>
                </span>
                <span className="text-xs">
                  {i.data_mode_label} · {i.quote_currency} per {i.unit} ·{" "}
                  {i.instrument_type.replace("_", " ")}
                </span>
                {!i.enabled && <span className="text-xs">Unavailable: {i.unavailable_reason}</span>}
              </label>
            ))}
            {unavailable.length > 0 && (
              <details className="text-xs">
                <summary>Unavailable combinations</summary>
                <ul>
                  {unavailable.map((u) => (
                    <li key={u.metal + u.venue}>
                      {u.metal} on {u.venue}: {u.reason}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </fieldset>
          <div className="flex flex-col gap-1">
            <label htmlFor="horizon" className="text-sm font-medium">
              Horizon
            </label>
            <select
              id="horizon"
              className={select}
              value={horizon}
              disabled={!selected}
              onChange={(e) => {
                setHorizon(e.target.value);
                invalidate();
              }}
            >
              <option value="">Choose a horizon</option>
              {selected?.supported_horizons.map((h) => (
                <option key={h} value={h}>
                  {h.replace("_", " ")}
                </option>
              ))}
            </select>
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor="profile" className="text-sm font-medium">
              Model profile
            </label>
            <select
              id="profile"
              className={select}
              value={profileId}
              onChange={(e) => {
                setProfileId(e.target.value);
                invalidate();
              }}
            >
              {models.data?.map((m) => (
                <option key={m.model_profile_id} value={m.model_profile_id}>
                  {m.display_name}
                </option>
              ))}
            </select>
            {profile && (
              <p className="text-xs">
                Provider Anthropic · model {profile.primary_model_id} · rate{" "}
                {profile.rate_card_version ?? "none"} · up to {profile.limits.run_model_calls} model
                calls, {profile.limits.run_tool_calls} tool calls,{" "}
                {profile.limits.run_active_seconds} s · {profile.fallback_policy}
              </p>
            )}
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor="question" className="text-sm font-medium">
              Question (optional)
            </label>
            <textarea
              id="question"
              rows={4}
              maxLength={MAX_Q}
              value={question}
              aria-describedby="q-count"
              className="rounded border border-gray-400 bg-transparent px-3 py-2"
              onChange={(e) => {
                setQuestion(e.target.value);
                invalidate();
              }}
            />
            <p id="q-count" className="text-xs">
              {question.length} / {MAX_Q} characters
            </p>
          </div>
          <Button type="submit" disabled={!canEstimate || pending}>
            {pending && !estimate ? "Estimating..." : "Get estimate"}
          </Button>
        </form>
      </Card>

      <div className="flex flex-col gap-4">
        <Card title="Estimate">
          {!estimate && (
            <p className="text-sm">Choose your research options, then get an estimate.</p>
          )}
          {estimate && (
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
              <dt>Estimated charge</dt>
              <dd>
                <DemoMoney value={estimate.estimated_charge} />
              </dd>
              <dt>Maximum charge</dt>
              <dd>
                <DemoMoney value={estimate.max_charge} />
              </dd>
              <dt>Real cost ceiling</dt>
              <dd>
                <RealCost value={estimate.max_real_cost} status="worst case" />
              </dd>
              <dt>Rate version</dt>
              <dd>{estimate.price_version}</dd>
              <dt>Models</dt>
              <dd>{estimate.approved_models.join(", ")}</dd>
              <dt>Limits</dt>
              <dd>
                {estimate.limits.run_model_calls} model / {estimate.limits.run_tool_calls} tool
                calls, {estimate.limits.run_active_seconds} s
              </dd>
              <dt>Expires in</dt>
              <dd aria-live="off">
                {expired
                  ? "expired"
                  : `${Math.floor(secondsLeft / 60)}:${String(secondsLeft % 60).padStart(2, "0")}`}
              </dd>
            </dl>
          )}
          {estimate && <p className="mt-2 text-xs">{estimate.estimate_basis}</p>}
        </Card>
        <Card title="Demo balance">
          <p>
            Available: <DemoMoney value={balance.data?.available} />
          </p>
          <p className="text-sm">
            Reserved for running research: <DemoMoney value={balance.data?.reserved} />
          </p>
        </Card>
        <ErrorText error={error} />
        {expired && <Alert kind="warning">The estimate expired. Get a new one.</Alert>}
        <Button onClick={() => void start()} disabled={!estimate || expired || pending}>
          {pending && estimate ? "Starting..." : "Start research"}
        </Button>
      </div>
    </div>
  );
}
