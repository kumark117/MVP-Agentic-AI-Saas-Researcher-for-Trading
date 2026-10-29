"use client";

/** Small accessible building blocks: labelled fields, buttons, alerts, money, time. */
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from "react";
import { useId } from "react";

import { ApiError } from "@/lib/api/client";
import { demoLabel, formatMoney, type Money } from "@/lib/money";

export function Button({
  variant = "primary",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "danger" }) {
  const styles = {
    primary: "bg-blue-700 text-white hover:bg-blue-800 disabled:bg-blue-300",
    secondary: "border border-gray-400 hover:bg-hover",
    danger: "bg-red-700 text-white hover:bg-red-800 disabled:bg-red-300",
  }[variant];
  return (
    <button
      type="button"
      {...props}
      className={`rounded px-4 py-2 text-sm font-medium disabled:cursor-not-allowed ${styles} ${className}`}
    />
  );
}

export function Field({
  label,
  hint,
  error,
  ...props
}: InputHTMLAttributes<HTMLInputElement> & { label: string; hint?: string; error?: string }) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  const errId = error ? `${id}-error` : undefined;
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      <input
        id={id}
        aria-invalid={error ? true : undefined}
        aria-describedby={[hintId, errId].filter(Boolean).join(" ") || undefined}
        className="rounded border border-gray-400 bg-transparent px-3 py-2"
        {...props}
      />
      {hint && (
        <p id={hintId} className="text-xs opacity-75">
          {hint}
        </p>
      )}
      {error && (
        <p id={errId} className="text-xs text-red-700 dark:text-red-400">
          Error: {error}
        </p>
      )}
    </div>
  );
}

export function Alert({
  kind = "info",
  children,
}: {
  kind?: "info" | "error" | "success" | "warning";
  children: ReactNode;
}) {
  const prefix = { info: "Note", error: "Error", success: "Done", warning: "Warning" }[kind];
  const styles = {
    info: "border-blue-600",
    error: "border-red-700",
    success: "border-green-700",
    warning: "border-amber-600",
  }[kind];
  return (
    <div role={kind === "error" ? "alert" : "status"} className={`border-l-4 px-3 py-2 ${styles}`}>
      <strong>{prefix}: </strong>
      {children}
    </div>
  );
}

export function ErrorText({ error }: { error: unknown }) {
  if (!error) return null;
  const msg =
    error instanceof ApiError
      ? `${error.problem.title ?? "Request failed"}. ${error.message}`
      : "Something went wrong. Please try again.";
  return <Alert kind="error">{msg}</Alert>;
}

/** Demo INR money: always labelled simulated. */
export function DemoMoney({ value }: { value: Money | null | undefined }) {
  return <span className="tabular-nums">{demoLabel(value)}</span>;
}

/** Real USD provider cost: labelled real, with its cost status. */
export function RealCost({ value, status }: { value: Money | null | undefined; status: string }) {
  return (
    <span className="tabular-nums">
      {value ? formatMoney(value) : "unknown"} <span className="text-xs">(real, {status})</span>
    </span>
  );
}

export function Timestamp({ iso }: { iso: string | null | undefined }) {
  if (!iso) return <span>not yet</span>;
  const d = new Date(iso);
  return (
    <time dateTime={iso} title={d.toISOString()}>
      {d.toLocaleString()}
    </time>
  );
}

export function Duration({ ms }: { ms: number | null | undefined }) {
  if (ms === null || ms === undefined) return <span>unknown</span>;
  return (
    <span className="tabular-nums">{ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`}</span>
  );
}

export function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title} className="rounded border border-gray-300 p-4 dark:border-gray-700">
      <h2 className="mb-3 text-lg font-semibold">{title}</h2>
      {children}
    </section>
  );
}

export function SimulationBanner() {
  return (
    <div role="note" className="bg-amber-100 px-4 py-2 text-sm text-amber-950">
      <strong>Simulation only.</strong> Demo credit has no cash value and no money is collected
      (money collected: ₹0.00). Real model and data costs are measured separately.
    </div>
  );
}

/** Model text: plain paragraphs only, never HTML (section 15: sanitised rendering). */
export function SafeText({ text }: { text: string }) {
  return (
    <>
      {text.split(/\n{2,}/).map((p, i) => (
        <p key={i} className="mb-2 whitespace-pre-wrap">
          {p}
        </p>
      ))}
    </>
  );
}
