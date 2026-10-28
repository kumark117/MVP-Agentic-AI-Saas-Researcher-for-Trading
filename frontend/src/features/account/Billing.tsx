"use client";

/**
 * Account and demo billing (R032, R036-R038; D022, D113-D115). Every value here is
 * simulated: no card, bank or UPI details are ever requested and money collected is ₹0.00.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import { useRef, useState, type FormEvent } from "react";

import { Alert, Button, Card, DemoMoney, ErrorText, Field, Timestamp } from "@/components/ui";
import { api, call, newIdempotencyKey, type Schemas } from "@/lib/api/client";
import { keys, useBalance } from "@/lib/api/queries";

export function AccountBilling() {
  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-semibold">Account and demo billing</h1>
      <Alert kind="info">
        Simulation only. Demo credit has no cash value and cannot be withdrawn.
      </Alert>
      <div className="grid gap-6 lg:grid-cols-2">
        <BalanceCard />
        <TopUp />
      </div>
      <Ledger />
      <div className="grid gap-6 lg:grid-cols-2">
        <RefundForm />
        <Receipts />
      </div>
    </div>
  );
}

function BalanceCard() {
  const b = useBalance();
  return (
    <Card title="Demo balance">
      <ErrorText error={b.error} />
      {b.data && (
        <dl className="grid grid-cols-2 gap-1 text-sm">
          <dt>Posted credit</dt>
          <dd>
            <DemoMoney value={b.data.posted_credit} />
          </dd>
          <dt>Posted debit</dt>
          <dd>
            <DemoMoney value={b.data.posted_debit} />
          </dd>
          <dt>Reserved</dt>
          <dd>
            <DemoMoney value={b.data.reserved} />
          </dd>
          <dt>Pending settlement</dt>
          <dd>
            <DemoMoney value={b.data.pending_settlement} />
          </dd>
          <dt className="font-semibold">Available</dt>
          <dd className="font-semibold">
            <DemoMoney value={b.data.available} />
          </dd>
          <dt>Money collected</dt>
          <dd>₹0.00</dd>
        </dl>
      )}
    </Card>
  );
}

function TopUp() {
  const router = useRouter();
  const [amount, setAmount] = useState("100.00");
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const key = useRef<{ amount: string; key: string } | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (pending) return;
    if (key.current?.amount !== amount) key.current = { amount, key: newIdempotencyKey() };
    setPending(true);
    setError(null);
    try {
      const co = await call<Schemas["Checkout"]>(() =>
        api.POST("/api/v1/billing/credit-checkouts", {
          body: { amount: { currency: "INR", amount }, return_path: "/account" },
          params: { header: { "Idempotency-Key": key.current!.key } },
        }),
      );
      router.push(co.hosted_url);
    } catch (err) {
      setError(err);
      setPending(false);
    }
  }

  return (
    <Card title="Simulated top-up">
      <form onSubmit={submit} className="flex flex-col gap-3">
        <Field
          label="Amount (INR, simulated)"
          inputMode="decimal"
          value={amount}
          pattern="^\d{1,6}(\.\d{1,2})?$"
          hint="Whole paise. Daily and per-checkout limits apply."
          onChange={(e) => setAmount(e.target.value)}
        />
        <ErrorText error={error} />
        <Button type="submit" disabled={pending}>
          {pending ? "Opening checkout..." : "Open demo checkout"}
        </Button>
      </form>
    </Card>
  );
}

function Ledger() {
  const [cursor, setCursor] = useState<string | undefined>();
  const ledger = useQuery({
    queryKey: keys.ledger(cursor),
    queryFn: () =>
      call<Schemas["Page_LedgerEntry_"]>(() =>
        api.GET("/api/v1/account/ledger", { params: { query: { limit: 20, cursor } } }),
      ),
  });
  return (
    <Card title="Demo ledger">
      <ErrorText error={ledger.error} />
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Immutable demo ledger entries</caption>
          <thead>
            <tr>
              <th scope="col">When</th>
              <th scope="col">Kind</th>
              <th scope="col">Description</th>
              <th scope="col">Amount</th>
            </tr>
          </thead>
          <tbody>
            {ledger.data?.items.map((e) => (
              <tr key={e.transaction_id} className="border-t border-gray-200 dark:border-gray-800">
                <td>
                  <Timestamp iso={e.posted_at} />
                </td>
                <td>{e.kind.replace("_", " ")}</td>
                <td>{e.description}</td>
                <td>
                  <DemoMoney value={e.amount} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-2 flex gap-2">
        {cursor && (
          <Button variant="secondary" onClick={() => setCursor(undefined)}>
            First page
          </Button>
        )}
        {ledger.data?.next_cursor && (
          <Button
            variant="secondary"
            onClick={() => setCursor(ledger.data?.next_cursor ?? undefined)}
          >
            Older
          </Button>
        )}
      </div>
    </Card>
  );
}

function RefundForm() {
  const qc = useQueryClient();
  const topups = useQuery({
    queryKey: ["ledger", "topups"],
    queryFn: () =>
      call<Schemas["Page_LedgerEntry_"]>(() =>
        api.GET("/api/v1/account/ledger", {
          params: { query: { event_type: "topup", limit: 50 } },
        }),
      ),
  });
  const [paymentId, setPaymentId] = useState("");
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [result, setResult] = useState<Schemas["Refund"] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const key = useRef<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    key.current ??= newIdempotencyKey();
    setPending(true);
    setError(null);
    try {
      const r = await call<Schemas["Refund"]>(() =>
        api.POST("/api/v1/billing/refund-requests", {
          body: { payment_id: paymentId, amount: { currency: "INR", amount }, reason },
          params: { header: { "Idempotency-Key": key.current! } },
        }),
      );
      setResult(r);
      key.current = null;
      void qc.invalidateQueries({ queryKey: keys.balance });
    } catch (err) {
      setError(err);
    } finally {
      setPending(false);
    }
  }

  const select = "rounded border border-gray-400 bg-transparent px-3 py-2";
  return (
    <Card title="Simulated refund">
      <form onSubmit={submit} className="flex flex-col gap-3">
        <div className="flex flex-col gap-1">
          <label htmlFor="refund-topup" className="text-sm font-medium">
            Top-up to refund
          </label>
          <select
            id="refund-topup"
            className={select}
            value={paymentId}
            onChange={(e) => {
              setPaymentId(e.target.value);
              key.current = null;
            }}
          >
            <option value="">Choose a top-up</option>
            {topups.data?.items.map(
              (t) =>
                t.checkout_id && (
                  <option key={t.transaction_id} value={t.checkout_id}>
                    {new Date(t.posted_at).toLocaleDateString()} · {t.amount.amount} INR (simulated)
                  </option>
                ),
            )}
          </select>
        </div>
        <Field
          label="Amount (INR, simulated)"
          inputMode="decimal"
          value={amount}
          onChange={(e) => {
            setAmount(e.target.value);
            key.current = null;
          }}
        />
        <Field
          label="Reason"
          value={reason}
          maxLength={500}
          onChange={(e) => {
            setReason(e.target.value);
            key.current = null;
          }}
        />
        <ErrorText error={error} />
        {result && (
          <Alert kind="success">
            Refund {result.status}. It reverses demo credit only; no money moves.
          </Alert>
        )}
        <Button type="submit" disabled={pending || !paymentId || !amount || !reason}>
          Request refund
        </Button>
      </form>
    </Card>
  );
}

function Receipts() {
  const receipts = useQuery({
    queryKey: keys.receipts,
    queryFn: () =>
      call<Schemas["Page_Receipt_"]>(() =>
        api.GET("/api/v1/billing/invoices", { params: { query: { limit: 20 } } }),
      ),
  });
  return (
    <Card title="Sample receipts">
      <p className="mb-2 text-xs">Simulation — not a tax invoice.</p>
      <ul className="flex flex-col gap-1 text-sm">
        {receipts.data?.items.map((r) => (
          <li key={r.receipt_id}>
            <Timestamp iso={r.issued_at} /> · {r.kind} · <DemoMoney value={r.amount} /> · {r.label}
          </li>
        ))}
        {receipts.data?.items.length === 0 && <li>No receipts yet.</li>}
      </ul>
    </Card>
  );
}

/** The local demo checkout page (hosted_url). Only an outcome is chosen here. */
export function DemoCheckout() {
  const { checkoutId } = useParams<{ checkoutId: string }>();
  const qc = useQueryClient();
  const router = useRouter();
  const checkout = useQuery({
    queryKey: keys.checkout(checkoutId),
    queryFn: () =>
      call<Schemas["Checkout"]>(() =>
        api.GET("/api/v1/billing/checkouts/{checkout_id}", {
          params: { path: { checkout_id: checkoutId } },
        }),
      ),
    refetchInterval: (q) =>
      q.state.data && ["paid", "failed", "expired"].includes(q.state.data.status) ? false : 1500,
  });
  const keysByOutcome = useRef<Record<string, string>>({});
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  async function simulate(outcome: "success" | "failure" | "pending") {
    if (pending) return; // a double click never sends twice; the key dedupes retries anyway
    keysByOutcome.current[outcome] ??= newIdempotencyKey();
    setPending(outcome);
    setError(null);
    try {
      await call(() =>
        api.POST("/api/v1/billing/checkouts/{checkout_id}/simulate", {
          params: {
            path: { checkout_id: checkoutId },
            header: { "Idempotency-Key": keysByOutcome.current[outcome] },
          },
          body: { outcome },
        }),
      );
      void qc.invalidateQueries({ queryKey: keys.checkout(checkoutId) });
    } catch (e) {
      setError(e);
    } finally {
      setPending(null);
    }
  }

  const c = checkout.data;
  const final = !!c && ["paid", "failed", "expired"].includes(c.status);
  if (final) void qc.invalidateQueries({ queryKey: keys.balance });
  return (
    <div className="mx-auto flex max-w-lg flex-col gap-4">
      <h1 className="text-2xl font-semibold">Demo checkout</h1>
      <Alert kind="warning">
        Simulation only. This page never asks for card, bank or UPI details and collects no money.
      </Alert>
      <ErrorText error={checkout.error ?? error} />
      {c && (
        <Card title="Checkout">
          <dl className="grid grid-cols-2 gap-1 text-sm">
            <dt>Amount</dt>
            <dd>
              <DemoMoney value={c.amount} />
            </dd>
            <dt>Status</dt>
            <dd className="font-semibold">{c.status}</dd>
            <dt>Credit applied</dt>
            <dd>{c.credit_applied ? "yes" : "no"}</dd>
            <dt>Expires</dt>
            <dd>
              <Timestamp iso={c.expires_at} />
            </dd>
            <dt>Money collected</dt>
            <dd>₹0.00</dd>
          </dl>
          {!final && (
            <div className="mt-3 flex flex-wrap gap-2">
              <Button onClick={() => void simulate("success")} disabled={!!pending}>
                Simulate success
              </Button>
              <Button
                variant="secondary"
                onClick={() => void simulate("pending")}
                disabled={!!pending}
              >
                Simulate pending
              </Button>
              <Button
                variant="danger"
                onClick={() => void simulate("failure")}
                disabled={!!pending}
              >
                Simulate failure
              </Button>
            </div>
          )}
          {final && (
            <div className="mt-3 flex flex-col gap-2">
              <Alert kind={c.status === "paid" ? "success" : "info"}>
                {c.status === "paid"
                  ? "Demo credit added (simulated)."
                  : `Checkout ${c.status}; no credit was added.`}
              </Alert>
              <Button variant="secondary" onClick={() => router.push(c.return_path)}>
                Back to account
              </Button>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
