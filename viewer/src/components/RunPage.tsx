import { useLoad } from "../load";
import { loadRun } from "../runData";
import type { RunSummary } from "../types";
import { Loaded, Verdict } from "./common";
import { Ledger } from "./Ledger";
import { Scorecard } from "./Scorecard";
import { Timeline } from "./Timeline";

export function RunHeader({ summary }: { summary: RunSummary }) {
  const run = summary.run;
  return (
    <header className="run-header">
      <h1>
        <code>{run.run_id}</code> <Verdict verdict={summary.scorecard?.verdict ?? null} />
      </h1>
      <p>
        {run.scenario_id}, configuration {run.config}, contract {run.contract}, seed {run.seed};{" "}
        {run.model_adapter} model {run.model}, {run.topology}; stopped at vt{" "}
        {run.stopped_vt ?? "not yet"}. Database {summary.database}.
      </p>
      {run.parent_run_id === null ? null : (
        <p className="experiment">
          A new experiment: a branch of {run.parent_run_id} at event {run.branch_point}.
        </p>
      )}
    </header>
  );
}

export function RunPage({ database }: { database: string }) {
  const load = useLoad(database, loadRun);
  return (
    <Loaded load={load}>
      {(data) => (
        <>
          <RunHeader summary={data.summary} />
          <Scorecard card={data.summary.scorecard} />
          <Timeline events={data.timeline.events} />
          <Ledger ledger={data.ledger} />
        </>
      )}
    </Loaded>
  );
}
