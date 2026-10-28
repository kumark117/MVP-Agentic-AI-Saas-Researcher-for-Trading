"use client";

/**
 * Profile and security (R010-R013): profile, change password, session inventory with a
 * "this device" marker, revoke one or all, deletion request. Sensitive actions need a
 * re-authentication within the last 5 minutes; the API answers 403
 * reauthentication-required and the user confirms their password once.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Alert, Button, Card, ErrorText, Field, Timestamp } from "@/components/ui";
import { api, ApiError, call } from "@/lib/api/client";
import { keys, useMe, useSessions } from "@/lib/api/queries";

function needsReauth(e: unknown): boolean {
  return (
    e instanceof ApiError &&
    e.status === 403 &&
    String(e.problem.type ?? "").endsWith("reauthentication-required")
  );
}

/** Runs an action; if the server wants fresh authentication, asks for the password once. */
function useSensitive() {
  const [pendingAction, setPendingAction] = useState<null | (() => Promise<void>)>(null);
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);

  async function run(action: () => Promise<void>) {
    setError(null);
    try {
      await action();
    } catch (e) {
      if (needsReauth(e)) setPendingAction(() => action);
      else setError(e);
    }
  }

  async function confirm(ev: FormEvent) {
    ev.preventDefault();
    try {
      await call(() => api.POST("/api/v1/auth/reauthenticate", { body: { password } }));
      const action = pendingAction;
      setPendingAction(null);
      setPassword("");
      if (action) await action();
    } catch (e) {
      setError(e);
    }
  }

  const dialog = pendingAction ? (
    <form
      onSubmit={confirm}
      className="flex flex-col gap-2 rounded border border-amber-600 p-3"
      aria-label="Confirm your password"
    >
      <Field
        label="Confirm your password to continue"
        type="password"
        autoComplete="current-password"
        autoFocus
        value={password}
        onChange={(e) => setPassword(e.target.value)}
      />
      <div className="flex gap-2">
        <Button type="submit">Confirm</Button>
        <Button variant="secondary" onClick={() => setPendingAction(null)}>
          Cancel
        </Button>
      </div>
    </form>
  ) : null;
  return { run, dialog, error };
}

export function SecurityPage() {
  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-semibold">Profile and security</h1>
      <div className="grid gap-6 lg:grid-cols-2">
        <Profile />
        <PasswordChange />
      </div>
      <Sessions />
      <Deletion />
    </div>
  );
}

function Profile() {
  const me = useMe();
  const qc = useQueryClient();
  const [name, setName] = useState<string | null>(null);
  const [tz, setTz] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [saved, setSaved] = useState(false);
  if (!me.data)
    return (
      <Card title="Profile">
        <ErrorText error={me.error} />
      </Card>
    );
  async function save(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSaved(false);
    try {
      await call(() =>
        api.PATCH("/api/v1/me", {
          body: { display_name: name ?? undefined, timezone: tz ?? undefined },
        }),
      );
      setSaved(true);
      void qc.invalidateQueries({ queryKey: keys.me });
    } catch (err) {
      setError(err);
    }
  }
  return (
    <Card title="Profile">
      <p className="mb-2 text-sm">Signed in as {me.data.user.email}</p>
      <form onSubmit={save} className="flex flex-col gap-3">
        <Field
          label="Display name"
          value={name ?? me.data.user.display_name}
          onChange={(e) => setName(e.target.value)}
        />
        <Field
          label="Timezone"
          hint="IANA name, for example Asia/Kolkata"
          value={tz ?? me.data.preferences.timezone}
          onChange={(e) => setTz(e.target.value)}
        />
        <ErrorText error={error} />
        {saved && <Alert kind="success">Profile saved.</Alert>}
        <Button type="submit">Save profile</Button>
      </form>
    </Card>
  );
}

function PasswordChange() {
  const { run, dialog, error } = useSensitive();
  const [pw, setPw] = useState("");
  const [done, setDone] = useState(false);
  return (
    <Card title="Change password">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void run(async () => {
            await call(() => api.POST("/api/v1/me/password", { body: { new_password: pw } }));
            setPw("");
            setDone(true);
          });
        }}
        className="flex flex-col gap-3"
      >
        <Field
          label="New password"
          type="password"
          autoComplete="new-password"
          hint="At least 12 characters. Other devices are signed out."
          value={pw}
          onChange={(e) => setPw(e.target.value)}
        />
        {dialog}
        <ErrorText error={error} />
        {done && (
          <Alert kind="success">Password changed. Your other sessions were signed out.</Alert>
        )}
        <Button type="submit" disabled={pw.length < 12}>
          Change password
        </Button>
      </form>
    </Card>
  );
}

function Sessions() {
  const sessions = useSessions();
  const qc = useQueryClient();
  const router = useRouter();
  const { run, dialog, error } = useSensitive();
  async function revoke(id: string, current: boolean) {
    await run(async () => {
      await call(() =>
        api.DELETE("/api/v1/me/sessions/{session_id}", { params: { path: { session_id: id } } }),
      );
      if (current) router.push("/login");
      void qc.invalidateQueries({ queryKey: keys.sessions });
    });
  }
  return (
    <Card title="Signed-in devices">
      <ErrorText error={sessions.error ?? error} />
      <ul className="flex flex-col gap-2 text-sm">
        {sessions.data?.items.map((s) => (
          <li
            key={s.session_id}
            className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-gray-200 pb-2 dark:border-gray-800"
          >
            <span className="font-medium">
              {s.device}
              {s.current ? " (this device)" : ""}
            </span>
            <span>
              Signed in <Timestamp iso={s.created_at} />
            </span>
            <span>
              Last active <Timestamp iso={s.last_seen_at} />
            </span>
            <span>
              Expires <Timestamp iso={s.expires_at} />
            </span>
            <Button variant="secondary" onClick={() => void revoke(s.session_id, s.current)}>
              {s.current ? "Sign out here" : "Revoke"}
            </Button>
          </li>
        ))}
      </ul>
      <div className="mt-3">
        <Button
          variant="danger"
          onClick={() =>
            void run(async () => {
              await call(() => api.POST("/api/v1/auth/logout-all"));
              router.push("/login");
            })
          }
        >
          Sign out everywhere
        </Button>
      </div>
      {dialog}
    </Card>
  );
}

function Deletion() {
  const { run, dialog, error } = useSensitive();
  const [scheduled, setScheduled] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  return (
    <Card title="Delete account">
      <p className="mb-2 text-sm">
        Deletion is scheduled after a 7-day grace period. Sessions end and eligible content is
        removed within 30 days; demo payment records follow product-data retention.
      </p>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={confirmed}
          onChange={(e) => setConfirmed(e.target.checked)}
        />
        I understand this deletes my account and research history.
      </label>
      {dialog}
      <ErrorText error={error} />
      {scheduled && (
        <Alert kind="success">Deletion scheduled for {new Date(scheduled).toLocaleString()}.</Alert>
      )}
      <div className="mt-2">
        <Button
          variant="danger"
          disabled={!confirmed}
          onClick={() =>
            void run(async () => {
              const r = await call<{ scheduled_at: string }>(() =>
                api.POST("/api/v1/me/deletion-requests", { body: { confirmation: true } }),
              );
              setScheduled(r.scheduled_at);
            })
          }
        >
          Request deletion
        </Button>
      </div>
    </Card>
  );
}
