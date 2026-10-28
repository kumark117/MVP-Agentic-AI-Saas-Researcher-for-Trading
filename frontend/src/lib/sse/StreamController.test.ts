import { describe, expect, it } from "vitest";

import { backoffMs, StreamController, type Probe, type SourceLike } from "./StreamController";

class FakeSource implements SourceLike {
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  onopen: ((ev: Event) => void) | null = null;
  listeners = new Map<string, (ev: MessageEvent) => void>();
  closed = false;
  constructor(readonly url: string) {}
  addEventListener(type: string, l: (ev: MessageEvent) => void): void {
    this.listeners.set(type, l);
  }
  close(): void {
    this.closed = true;
  }
  emit(type: string, seq: number, scope = "r"): void {
    const data = JSON.stringify({ event_id: `${scope}:${seq}`, sequence: seq, payload: {} });
    this.listeners.get(type)?.({ data, lastEventId: `${scope}:${seq}` } as MessageEvent);
  }
}

function setup(probe: Probe = async () => ({ kind: "ok", latestEventId: null })) {
  const sources: FakeSource[] = [];
  const timers: Array<() => void> = [];
  const events: string[] = [];
  const statuses: string[] = [];
  const c = new StreamController({
    url: "/api/v1/research/runs/r/events",
    eventTypes: ["run.started", "run.finished", "stream.reset_required"],
    onEvent: (e) => events.push(e.id),
    onStatus: (s) => statuses.push(s),
    probe,
    source: (u) => {
      const s = new FakeSource(u);
      sources.push(s);
      return s;
    },
    random: () => 0.5,
    schedule: (fn) => timers.push(fn),
    closeOn: ["run.finished"],
  });
  return { c, sources, timers, events, statuses };
}

describe("StreamController", () => {
  it("deduplicates by event id and resumes after the last one", () => {
    const { c, sources, timers, events } = setup();
    c.start();
    sources[0].emit("run.started", 1);
    sources[0].emit("run.started", 1);
    sources[0].emit("run.started", 2);
    sources[0].onerror?.(new Event("error"));
    timers.shift()?.();
    expect(sources[1].url).toBe("/api/v1/research/runs/r/events?after=r%3A2");
    sources[1].emit("run.started", 2); // replayed again by the server
    sources[1].emit("run.started", 3);
    expect(events).toEqual(["r:1", "r:2", "r:3"]);
  });

  it("closes after run.finished", () => {
    const { c, sources, statuses } = setup();
    c.start();
    sources[0].emit("run.finished", 9);
    expect(sources[0].closed).toBe(true);
    expect(statuses.at(-1)).toBe("closed");
  });

  it("detects an expired session instead of reconnecting forever", async () => {
    const { c, sources, timers, statuses } = setup(async () => ({ kind: "unauthenticated" }));
    c.start();
    for (let i = 0; i < 3; i++) {
      sources.at(-1)!.onerror?.(new Event("error"));
      await Promise.resolve();
      timers.shift()?.();
    }
    sources.at(-1)!.onerror?.(new Event("error"));
    await new Promise((r) => setTimeout(r, 0));
    expect(statuses.at(-1)).toBe("unauthenticated");
    expect(timers.length).toBe(0);
  });

  it("resyncs from the snapshot on reset_required", async () => {
    const { c, sources } = setup(async () => ({ kind: "ok", latestEventId: "r:40" }));
    c.start();
    sources[0].listeners.get("stream.reset_required")?.({
      data: JSON.stringify({ payload: { reason: "buffer_overflow" } }),
    } as MessageEvent);
    await new Promise((r) => setTimeout(r, 0));
    expect(sources[1].url).toContain("after=r%3A40");
  });

  it("backs off with jitter between 1 and 30 seconds", () => {
    expect(backoffMs(1, () => 0)).toBe(1000);
    expect(backoffMs(3, () => 1)).toBe(4000);
    expect(backoffMs(20, () => 1)).toBe(30000);
  });
});
