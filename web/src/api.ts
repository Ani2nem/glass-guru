// Typed client. Errors are surfaced with the server's own remedy text rather than a
// status code: the API already says what to do about each failure, and rewording it
// here would only lose that.

import type {
  Intake,
  Message,
  Note,
  Plan,
  Repair,
  Triage,
  World,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly remedy: string = "",
    readonly status: number = 0,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    let detail = response.statusText;
    let remedy = "";
    try {
      const body = await response.json();
      const payload = body.detail ?? body;
      detail = payload.detail ?? payload.error ?? detail;
      remedy = payload.remedy ?? "";
    } catch {
      /* a non-JSON error body is still an error */
    }
    throw new ApiError(detail, remedy, response.status);
  }
  return (await response.json()) as T;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });

export const api = {
  world: () => request<World>("/api/world"),
  plan: () => request<Plan | null>("/api/plan"),
  commit: () => post<Plan>("/api/plan/commit"),
  repair: () => post<Repair>("/api/repair"),
  applyRepair: (strategy: string, force = false) =>
    post<Plan>(`/api/repair/apply?strategy=${encodeURIComponent(strategy)}&force=${force}`),
  recordEvent: (event: Record<string, unknown>) =>
    post<{ event_id: string; type: string }>("/api/events", event),
  triage: (text: string) => post<Triage>("/api/triage", { text }),
  acceptTriage: (events: Record<string, unknown>[]) =>
    post<{ recorded: number }>("/api/triage/accept", { events }),
  intake: (text: string) => post<Intake>("/api/intake", { text }),
  /** One box: the classifier picks the agent, `kind` overrides it. */
  note: (text: string, kind?: "booking" | "disruption") =>
    post<Note>(`/api/note${kind ? `?kind=${kind}` : ""}`, { text }),
  comms: (strategy: string) =>
    post<Message[]>(`/api/comms?strategy=${encodeURIComponent(strategy)}`),
};

/** How often to re-check when streaming is off. Slow enough to be nearly free, fast
 * enough that a change made in the CLI shows up before you switch windows. */
const POLL_MS = 10_000;

function stream(onChange: () => void): () => void {
  const source = new EventSource("/api/stream");
  source.onmessage = (event) => {
    const payload = JSON.parse(event.data) as { type: string };
    if (payload.type === "plan" || payload.type === "world") onChange();
  };
  return () => source.close();
}

function poll(onChange: () => void): () => void {
  const timer = setInterval(onChange, POLL_MS);
  return () => clearInterval(timer);
}

/**
 * Live updates, so a change made from the CLI or an agent shows up here too.
 *
 * The server chooses the mechanism, because the client cannot: a stream works
 * perfectly well on Lambda, it is just billed for every second it stays open, and
 * there is no failure to detect and fall back from. Running locally it streams;
 * deployed it polls.
 */
export function subscribe(onChange: () => void): () => void {
  let stop = () => {};
  let cancelled = false;

  void (async () => {
    let mode = "sse";
    try {
      const health = await request<{ stream?: string }>("/api/health");
      mode = health.stream ?? "sse";
    } catch {
      // Unreachable server, or an older one that does not say. Streaming is the
      // right guess for both: it is what a local dev server does.
    }
    if (cancelled) return;
    stop = mode === "poll" ? poll(onChange) : stream(onChange);
  })();

  return () => {
    cancelled = true;
    stop();
  };
}
