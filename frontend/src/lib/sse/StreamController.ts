/**
 * StreamController (build prompt section 14; D084-D090): one per mounted stream scope.
 *
 * - Deduplicates by event_id (delivery is at least once).
 * - Reconnects with jittered backoff from 1 s to 30 s, resuming from the last event id
 *   (EventSource sends Last-Event-ID itself; a brand-new source uses ?after=).
 * - After repeated failures it does an authenticated snapshot read to tell
 *   "session expired" (stop, redirect to login) from "cursor expired" (refetch the
 *   snapshot and reopen from its latest_event_id). It never reconnects endlessly.
 * - stream.reset_required: resync from the snapshot, then reopen.
 */

export type StreamEvent = {
  type: string;
  id: string;
  sequence: number;
  payload: Record<string, unknown>;
  occurredAt: string;
};

export type Probe = () => Promise<
  { kind: "ok"; latestEventId: string | null } | { kind: "unauthenticated" } | { kind: "gone" }
>;

export interface SourceLike {
  onmessage: ((ev: MessageEvent) => void) | null;
  onerror: ((ev: Event) => void) | null;
  onopen: ((ev: Event) => void) | null;
  addEventListener(type: string, listener: (ev: MessageEvent) => void): void;
  close(): void;
}

export type SourceFactory = (url: string) => SourceLike;

export type ControllerOptions = {
  url: string; // e.g. /api/v1/research/runs/{id}/events
  eventTypes: readonly string[];
  onEvent: (e: StreamEvent) => void;
  onStatus?: (s: "connecting" | "open" | "closed" | "unauthenticated" | "failed") => void;
  probe: Probe; // authenticated snapshot read
  source?: SourceFactory;
  random?: () => number;
  schedule?: (fn: () => void, ms: number) => unknown;
  maxFailures?: number;
  closeOn?: readonly string[]; // e.g. ["run.finished"] for run streams
};

export const RUN_EVENT_TYPES = [
  "run.queued",
  "run.started",
  "node.started",
  "tool.started",
  "tool.completed",
  "tool.failed",
  "llm.completed",
  "validation.completed",
  "activity.appended",
  "run.cancel_requested",
  "report.ready",
  "billing.updated",
  "run.finished",
  "stream.reset_required",
] as const;

export const ACCOUNT_EVENT_TYPES = [
  "run.updated",
  "balance.updated",
  "export.ready",
  "security.session_revoked",
  "stream.reset_required",
] as const;

export function backoffMs(attempt: number, random: () => number = Math.random): number {
  const base = Math.min(30_000, 1_000 * 2 ** Math.max(0, attempt - 1));
  const jittered = base / 2 + random() * (base / 2);
  return Math.max(1_000, Math.min(30_000, Math.round(jittered)));
}

export class StreamController {
  private seen = new Set<string>();
  private lastId: string | null = null;
  private src: SourceLike | null = null;
  private failures = 0;
  private stopped = false;
  private readonly o: Required<
    Pick<ControllerOptions, "random" | "schedule" | "maxFailures" | "closeOn">
  > &
    ControllerOptions;

  constructor(options: ControllerOptions) {
    this.o = {
      random: Math.random,
      schedule: (fn, ms) => setTimeout(fn, ms),
      maxFailures: 4,
      closeOn: [],
      ...options,
    };
  }

  get lastEventId(): string | null {
    return this.lastId;
  }

  start(fromEventId: string | null = null): void {
    this.stopped = false;
    if (fromEventId) this.lastId = fromEventId;
    this.open();
  }

  stop(): void {
    this.stopped = true;
    this.src?.close();
    this.src = null;
    this.o.onStatus?.("closed");
  }

  private url(): string {
    if (!this.lastId) return this.o.url;
    const sep = this.o.url.includes("?") ? "&" : "?";
    return `${this.o.url}${sep}after=${encodeURIComponent(this.lastId)}`;
  }

  private open(): void {
    if (this.stopped) return;
    this.o.onStatus?.("connecting");
    const make: SourceFactory =
      this.o.source ?? ((u) => new EventSource(u, { withCredentials: true }) as SourceLike);
    const src = make(this.url());
    this.src = src;
    src.onopen = () => {
      this.failures = 0;
      this.o.onStatus?.("open");
    };
    for (const type of this.o.eventTypes) {
      src.addEventListener(type, (ev) => this.handle(type, ev));
    }
    src.onerror = () => {
      src.close();
      if (this.src === src) this.src = null;
      void this.recover();
    };
  }

  private handle(type: string, ev: MessageEvent): void {
    let data: {
      event_id?: string;
      sequence?: number;
      payload?: Record<string, unknown>;
      occurred_at?: string;
    };
    try {
      data = JSON.parse(String(ev.data));
    } catch {
      return;
    }
    if (type === "stream.reset_required") {
      this.src?.close();
      this.src = null;
      void this.resync();
      return;
    }
    const id = data.event_id ?? ev.lastEventId;
    if (!id || this.seen.has(id)) return;
    this.seen.add(id);
    this.lastId = id;
    this.o.onEvent({
      type,
      id,
      sequence: data.sequence ?? 0,
      payload: data.payload ?? {},
      occurredAt: data.occurred_at ?? "",
    });
    if (this.o.closeOn.includes(type)) this.stop();
  }

  private async resync(): Promise<void> {
    const r = await this.o.probe();
    if (r.kind === "unauthenticated") {
      this.stopped = true;
      this.o.onStatus?.("unauthenticated");
      return;
    }
    this.lastId = r.kind === "ok" ? r.latestEventId : null;
    this.failures = 0;
    this.open();
  }

  private async recover(): Promise<void> {
    if (this.stopped) return;
    this.failures += 1;
    if (this.failures >= this.o.maxFailures) {
      // Native EventSource hides non-200 bodies: ask the API what happened.
      const r = await this.o.probe();
      if (r.kind === "unauthenticated") {
        this.stopped = true;
        this.o.onStatus?.("unauthenticated");
        return;
      }
      if (r.kind === "gone") {
        this.stopped = true;
        this.o.onStatus?.("failed");
        return;
      }
      this.lastId = r.latestEventId ?? this.lastId; // cursor may have expired
      if (this.failures >= this.o.maxFailures * 2) {
        this.stopped = true;
        this.o.onStatus?.("failed");
        return;
      }
    }
    this.o.schedule(() => this.open(), backoffMs(this.failures, this.o.random));
  }
}
