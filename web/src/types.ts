// Mirrors glass_guru.api.models. Hand-written rather than generated: the board
// consumes a deliberately small slice, and a generated client would pull in the whole
// surface plus its churn.

export interface Stop {
  job_id: string;
  customer_name: string;
  service_type: string;
  arrival: string;
  departure: string;
  start_minute: number;
  end_minute: number;
  travel_minutes: number;
  travel_miles: number;
  crew_size: number;
  commitment_state: string;
  gap_minutes: number;
  lat: number;
  lon: number;
}

export interface Route {
  crew_id: string;
  date: string;
  worker_names: string[];
  van_id: string;
  stops: Stop[];
  travel_minutes: number;
  travel_miles: number;
  idle_minutes: number;
  utilization: number;
  overtime_minutes: number;
}

export interface Unserved {
  job_id: string;
  customer_name: string;
  reason: string;
  detail: string;
  is_failure: boolean;
}

export interface Cost {
  travel_labor: number;
  vehicle: number;
  overtime: number;
  lateness: number;
  unserved: number;
  total: number;
}

export interface Plan {
  plan_id: string;
  content_hash: string;
  horizon_start: string;
  horizon_end: string;
  routes: Route[];
  unserved: Unserved[];
  cost: Cost;
  feasible: boolean;
  violations: string[];
  depot: number[];
}

export interface Worker {
  id: string;
  name: string;
  certifications: string[];
  shift: string;
  available: boolean;
  overtime_eligible: boolean;
}

export interface Van {
  id: string;
  label: string;
  available: boolean;
  stock: Record<string, number>;
}

export interface Job {
  id: string;
  customer_name: string;
  service_type: string;
  duration_minutes: number;
  crew_size: number;
  certifications: string[];
  commitment_state: string;
  commitment_cost: number;
  window: string;
  lat: number;
  lon: number;
}

export interface World {
  as_of: string;
  workers: Worker[];
  vans: Van[];
  jobs: Job[];
  committed_plan_id: string;
  calibration_warning: string;
}

export interface Change {
  job_id: string;
  customer_name: string;
  kind: string;
  description: string;
  needs_customer_call: boolean;
}

export interface Candidate {
  strategy: string;
  description: string;
  jobs_served: number;
  changes: number;
  customer_calls: number;
  blast_radius: string;
  autonomy: string;
  autonomy_reasons: string[];
  diff: Change[];
  recommended: boolean;
}

export interface Repair {
  baseline_plan_id: string;
  candidates: Candidate[];
  recommended: string;
  rationale: string;
  chosen_by: string;
}

export interface Slot {
  date: string;
  window: string;
  marginal_cost: number;
  crew: string;
  reason: string;
}

export interface Draft {
  customer_name: string;
  phone: string;
  address: string;
  service_type: string;
  duration_minutes: number;
  duration_confidence: number;
  crew_size: number;
  certifications: string[];
  commitment_cost: number;
  commitment_quotes: string[];
  lead_time_days: number;
  site_notes: string;
  lat: number | null;
  lon: number | null;
}

export interface Intake {
  draft: Draft;
  bookable: boolean;
  missing: string[];
  ask_next: string[];
  slots: Slot[];
  repairs: number;
  note: string;
}

export interface Triage {
  state: string;
  summary: string;
  events: Record<string, unknown>[];
  question: string;
  unknown_targets: string[];
  rejected: string[];
  repairs: number;
}

export interface Message {
  job_id: string;
  channel: string;
  body: string;
  grounded: boolean;
  issues: string[];
}
