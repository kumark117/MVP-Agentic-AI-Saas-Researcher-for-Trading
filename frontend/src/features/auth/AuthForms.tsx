"use client";

/**
 * Register, verify email, login, forgot and reset password (R008, R010-R013).
 * One-time tokens arrive in the URL fragment (#token=...), which never reaches a server
 * or a Referer header; the page posts it to the API (ADR-012).
 */
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { z } from "zod";

import { Alert, Button, ErrorText, Field } from "@/components/ui";
import { api, call, refreshCsrf } from "@/lib/api/client";

const email = z.string().trim().email("Enter a valid email address");
const password = z.string().min(12, "Use at least 12 characters").max(200);

function fragmentToken(): string | null {
  if (typeof window === "undefined") return null;
  const m = /token=([\w-]+)/.exec(window.location.hash);
  return m ? m[1] : null;
}

function Shell({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <main id="main" className="mx-auto flex w-full max-w-md flex-col gap-4 px-6 py-12">
      <h1 className="text-2xl font-semibold">{title}</h1>
      {children}
      <p className="text-xs opacity-75">
        AlloyIQ is research only: no orders, no investment capital. Payments are simulated.
      </p>
    </main>
  );
}

function useSubmit<T>(fn: () => Promise<T>) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const run = async (e?: FormEvent) => {
    e?.preventDefault();
    setPending(true);
    setError(null);
    try {
      return await fn();
    } catch (err) {
      setError(err);
      return undefined;
    } finally {
      setPending(false);
    }
  };
  return { pending, error, run };
}

export function LoginForm() {
  const router = useRouter();
  const [values, setValues] = useState({ email: "", password: "" });
  const [fieldErr, setFieldErr] = useState<Record<string, string>>({});
  const { pending, error, run } = useSubmit(async () => {
    const parsed = z
      .object({ email, password: z.string().min(1, "Enter your password") })
      .safeParse(values);
    if (!parsed.success) {
      setFieldErr(
        Object.fromEntries(parsed.error.issues.map((i) => [String(i.path[0]), i.message])),
      );
      return;
    }
    setFieldErr({});
    await call(() => api.POST("/api/v1/auth/login", { body: parsed.data }));
    router.push("/research");
  });
  return (
    <Shell title="Sign in">
      <form onSubmit={run} className="flex flex-col gap-3" noValidate>
        <Field
          label="Email"
          type="email"
          autoComplete="email"
          value={values.email}
          error={fieldErr.email}
          onChange={(e) => setValues({ ...values, email: e.target.value })}
        />
        <Field
          label="Password"
          type="password"
          autoComplete="current-password"
          value={values.password}
          error={fieldErr.password}
          onChange={(e) => setValues({ ...values, password: e.target.value })}
        />
        <ErrorText error={error} />
        <Button type="submit" disabled={pending}>
          {pending ? "Signing in..." : "Sign in"}
        </Button>
      </form>
      <p className="text-sm">
        <Link className="underline" href="/forgot-password">
          Forgot password?
        </Link>{" "}
        ·{" "}
        <Link className="underline" href="/register">
          Create an account
        </Link>
      </p>
    </Shell>
  );
}

export function RegisterForm() {
  const [values, setValues] = useState({ email: "", password: "", display_name: "" });
  const [fieldErr, setFieldErr] = useState<Record<string, string>>({});
  const [done, setDone] = useState(false);
  const { pending, error, run } = useSubmit(async () => {
    const parsed = z
      .object({ email, password, display_name: z.string().trim().min(1, "Enter a name").max(80) })
      .safeParse(values);
    if (!parsed.success) {
      setFieldErr(
        Object.fromEntries(parsed.error.issues.map((i) => [String(i.path[0]), i.message])),
      );
      return;
    }
    setFieldErr({});
    await call(() =>
      api.POST("/api/v1/auth/register", {
        body: { ...parsed.data, terms_version: "2026-10", privacy_version: "2026-10" },
      }),
    );
    setDone(true);
  });
  if (done) {
    return (
      <Shell title="Check your email">
        <Alert kind="success">
          If this address can be registered, we sent a verification link. It expires in 30 minutes.
        </Alert>
        <Link className="underline" href="/login">
          Back to sign in
        </Link>
      </Shell>
    );
  }
  return (
    <Shell title="Create an account">
      <form onSubmit={run} className="flex flex-col gap-3" noValidate>
        <Field
          label="Name"
          autoComplete="name"
          value={values.display_name}
          error={fieldErr.display_name}
          onChange={(e) => setValues({ ...values, display_name: e.target.value })}
        />
        <Field
          label="Email"
          type="email"
          autoComplete="email"
          value={values.email}
          error={fieldErr.email}
          onChange={(e) => setValues({ ...values, email: e.target.value })}
        />
        <Field
          label="Password"
          type="password"
          autoComplete="new-password"
          hint="At least 12 characters."
          value={values.password}
          error={fieldErr.password}
          onChange={(e) => setValues({ ...values, password: e.target.value })}
        />
        <p className="text-xs">
          By creating an account you accept the demo terms: research only, simulated payments, and
          demo credit with no cash value.
        </p>
        <ErrorText error={error} />
        <Button type="submit" disabled={pending}>
          {pending ? "Creating..." : "Create account"}
        </Button>
      </form>
      <Link className="text-sm underline" href="/login">
        I already have an account
      </Link>
    </Shell>
  );
}

export function VerifyEmail() {
  const [state, setState] = useState<"working" | "ok" | "failed">("working");
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    const token = fragmentToken();
    if (!token) {
      queueMicrotask(() => setState("failed")); // outside the effect body
      return;
    }
    history.replaceState(null, "", window.location.pathname); // drop the token from the URL
    call(() => api.POST("/api/v1/auth/verify-email", { body: { token } }))
      .then(() => setState("ok"))
      .catch((e) => {
        setError(e);
        setState("failed");
      });
  }, []);
  return (
    <Shell title="Verify your email">
      {state === "working" && <p>Verifying...</p>}
      {state === "ok" && (
        <Alert kind="success">
          Your email is verified and your simulated opening credit is ready.{" "}
          <Link className="underline" href="/login">
            Sign in
          </Link>
        </Alert>
      )}
      {state === "failed" &&
        (error ? <ErrorText error={error} /> : <Alert kind="error">This link is invalid.</Alert>)}
    </Shell>
  );
}

export function ForgotPassword() {
  const [value, setValue] = useState("");
  const [done, setDone] = useState(false);
  const { pending, error, run } = useSubmit(async () => {
    await call(() => api.POST("/api/v1/auth/forgot-password", { body: { email: value } }));
    setDone(true);
  });
  return (
    <Shell title="Reset your password">
      {done ? (
        <Alert kind="success">If an account exists for this address, we sent a reset link.</Alert>
      ) : (
        <form onSubmit={run} className="flex flex-col gap-3">
          <Field
            label="Email"
            type="email"
            autoComplete="email"
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
          <ErrorText error={error} />
          <Button type="submit" disabled={pending}>
            Send reset link
          </Button>
        </form>
      )}
    </Shell>
  );
}

export function ResetPassword() {
  const [value, setValue] = useState("");
  const [err, setErr] = useState<string>();
  const [done, setDone] = useState(false);
  const { pending, error, run } = useSubmit(async () => {
    const p = password.safeParse(value);
    if (!p.success) {
      setErr(p.error.issues[0].message);
      return;
    }
    const token = fragmentToken();
    if (!token) throw new Error("missing token");
    await call(() =>
      api.POST("/api/v1/auth/reset-password", { body: { token, new_password: value } }),
    );
    await refreshCsrf();
    setDone(true);
  });
  return (
    <Shell title="Choose a new password">
      {done ? (
        <Alert kind="success">
          Password changed and every device signed out.{" "}
          <Link className="underline" href="/login">
            Sign in
          </Link>
        </Alert>
      ) : (
        <form onSubmit={run} className="flex flex-col gap-3">
          <Field
            label="New password"
            type="password"
            autoComplete="new-password"
            value={value}
            error={err}
            onChange={(e) => setValue(e.target.value)}
          />
          <ErrorText error={error} />
          <Button type="submit" disabled={pending}>
            Set password
          </Button>
        </form>
      )}
    </Shell>
  );
}
