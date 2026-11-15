// The JSON the viewer's read-only API answers (src/fivefold/viewer/app.py, record 0012).

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

export interface RegistryRun {
  database: string;
  run_id: string;
  scenario_id: string;
  config: string;
  contract: string;
  seed: number;
  created_wall_at: string;
}

export interface RunList {
  registry: string;
  runs: RegistryRun[];
}

export interface Run {
  run_id: string;
  scenario_id: string;
  scenario_digest: string;
  config: string;
  contract: string;
  seed: number;
  topology: string;
  model_adapter: string;
  model: string;
  cassette_digest: string | null;
  parent_run_id: string | null;
  branch_point: number | null;
  params: Json;
  stopped_vt: number | null;
}

export interface Failure {
  code: string;
  intent_id: string | null;
  effect_ids: string[];
  detail: string;
}

export interface PropertyResult {
  property: number;
  name: string;
  verdict: "pass" | "fail" | "not_evaluated";
  reason_codes: string[];
  failures: Failure[];
  metrics: Record<string, number>;
}

export interface ExcusedIntent {
  intent_id: string;
  excuse: string;
  approval_ids: string[];
  detail: string;
}

export interface ClaimJudgment {
  intent_id: string;
  status: string;
  claimed_effects: string[];
  knowledge: string;
  known_effects: string[];
  ledger_effects: string[];
  passed: boolean;
  reason_code: string | null;
}

export interface CountedReport {
  seq: number;
  worker: string;
  accepted_by: string;
  message_seq: number;
  t_r: number;
  claims: ClaimJudgment[];
}

export interface Scorecard {
  verdict: "pass" | "pass_unresolved" | "fail";
  evaluated: number[];
  properties: PropertyResult[];
  excused_intents: ExcusedIntent[];
  report: CountedReport | null;
  metrics: Record<string, number>;
  code_version: string | null;
}

export interface RunSummary {
  database: string;
  registry: RegistryRun;
  run: Run;
  scorecard: Scorecard | null;
}

export interface Message {
  seq: number;
  hop: string;
  kind: "request" | "answer" | "notification";
  sender: string;
  receiver: string;
  api: string | null;
  correlation: number | null;
  intent_id: string | null;
  idempotency_key: string | null;
  answer_class: string | null;
  body: Json;
  sent_vt: number;
  fate: "delivered" | "dropped" | null;
  drop_reason: string | null;
  delivered_seq: number | null;
  delivered_vt: number | null;
  copy_of: number | null;
}

export interface Fault {
  seq: number;
  fault: string;
  variant: string | null;
  point: string;
  occurrence: number;
  action: string;
  target_seq: number | null;
  component: string | null;
  voided: number[];
  params: Json;
  vt: number;
}

export interface ActorAction {
  seq: number;
  actor: string;
  action: string;
  subject: string;
  details: Json;
  vt: number;
}

export interface Report {
  seq: number;
  worker: string;
  report: Json;
  valid: boolean;
  errors: Json;
  accepted_by: string;
  message_seq: number | null;
  vt: number;
}

export interface ActionEvent {
  seq: number;
  action_id: string;
  kind: "transition" | "note";
  from_state: string | null;
  to_state: string;
  reason: string;
  details: Json;
  vt: number;
}

interface EventBase {
  seq: number;
  vt: number;
  shares_vt: boolean;
}

export type TimelineEvent =
  | (EventBase & { source: "harness"; kind: "message"; message: Message })
  | (EventBase & { source: "harness"; kind: "fault"; fault: Fault })
  | (EventBase & { source: "harness"; kind: "actor"; actor: ActorAction })
  | (EventBase & { source: "harness"; kind: "report"; report: Report })
  | (EventBase & { source: "gateway"; kind: "action_event"; action_event: ActionEvent });

export interface Timeline {
  database: string;
  events: TimelineEvent[];
}

export interface Effect {
  effect_id: string;
  intent_id: string;
  operation: string;
  args: { [key: string]: Json };
  approval_id: string;
  basis: { [key: string]: Json };
  idempotency_key: string | null;
  client_ref: string | null;
  received_seq: number;
  commit_seq: number;
  committed_vt: number;
}

export interface FactVersion {
  key: string;
  version: number;
  value: { [key: string]: Json };
  valid_from_seq: number;
  valid_from_vt: number;
  cause: "initial" | "effect" | "control";
  cause_effect_id: string | null;
  cause_actor: string | null;
}

export interface Ledger {
  database: string;
  effects: Effect[];
  facts: FactVersion[];
}

export interface ApiError {
  error: string;
  detail: string;
}
