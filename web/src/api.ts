// Typed client. Errors are surfaced with the server's own remedy text rather than a
// status code: the API already says what to do about each failure, and rewording it
// here would only lose that.

import type {
  Intake,
  Message,
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
  comms: (strategy: string) =>
    post<Message[]>(`/api/comms?strategy=${encodeURIComponent(strategy)}`),
};

/** Live updates, so a change made from the CLI or an agent shows up here too. */
export function subscribe(onChange: () => void): () => void {
  const source = new EventSource("/api/stream");
  source.onmessage = (event) => {
    const payload = JSON.parse(event.data) as { type: string };
    if (payload.type === "plan" || payload.type === "world") onChange();
  };
  return () => source.close();
}
