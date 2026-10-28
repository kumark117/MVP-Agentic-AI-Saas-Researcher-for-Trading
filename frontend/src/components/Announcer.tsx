"use client";

/**
 * One polite aria-live region for the whole app (section 15): progress summaries such as
 * "Price branch complete, 2 of 3", never one announcement per token or event.
 */
import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";

const Ctx = createContext<(message: string) => void>(() => {});

export function Announcer({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState("");
  const last = useRef(0);
  const announce = useCallback((m: string) => {
    const now = Date.now();
    if (now - last.current < 1500) return; // throttle: summaries, not a stream of updates
    last.current = now;
    setMessage(m);
  }, []);
  return (
    <Ctx.Provider value={announce}>
      {children}
      <div aria-live="polite" role="status" className="sr-only">
        {message}
      </div>
    </Ctx.Provider>
  );
}

export function useAnnounce(): (message: string) => void {
  return useContext(Ctx);
}
