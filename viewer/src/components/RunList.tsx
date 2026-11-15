import { useState } from "react";

import { api } from "../api";
import { useLoad } from "../load";
import { href } from "../route";
import type { RegistryRun, RunList as RunListData } from "../types";
import { pairs } from "../views";
import { Loaded } from "./common";

const loadRuns = (_key: string, signal: AbortSignal) => api.runs(signal);

function when(created: string): string {
  const date = new Date(created);
  return Number.isNaN(date.getTime()) ? created : date.toLocaleString();
}

function Runs({ list }: { list: RunListData }) {
  const [picked, setPicked] = useState<string[]>([]);
  const pick = (database: string) =>
    setPicked((before) =>
      before.includes(database)
        ? before.filter((name) => name !== database)
        : [...before, database].slice(-2),
    );
  const suggested = pairs(list.runs);
  if (list.runs.length === 0) {
    return (
      <p className="muted">
        The registry {list.registry} lists no run yet. Run <code>make demo</code>, then reload.
      </p>
    );
  }
  return (
    <>
      <section className="panel" aria-label="Side by side">
        <h2>Side by side</h2>
        {suggested.length === 0 ? (
          <p className="muted">No B0 run has a G2 run of the same scenario, contract level and seed.</p>
        ) : (
          <ul className="pairs">
            {suggested.map(({ b0, g2 }) => (
              <li key={`${b0.database}/${g2.database}`}>
                <a href={href({ page: "compare", left: b0.database, right: g2.database })}>
                  {b0.scenario_id}, {b0.contract}, seed {b0.seed}: B0 beside G2
                </a>{" "}
                <span className="muted">
                  ({b0.database} and {g2.database})
                </span>
              </li>
            ))}
          </ul>
        )}
        <p>
          Or pick any two runs below:{" "}
          {picked.length === 2 ? (
            <a href={href({ page: "compare", left: picked[0]!, right: picked[1]! })}>
              compare {picked[0]} and {picked[1]}
            </a>
          ) : (
            <span className="muted">{2 - picked.length} more to pick</span>
          )}
        </p>
      </section>
      <section className="panel" aria-label="Runs">
        <h2>Runs</h2>
        <table className="runs">
          <thead>
            <tr>
              <th>Pick</th>
              <th>Database</th>
              <th>Run</th>
              <th>Scenario</th>
              <th>Config</th>
              <th>Contract</th>
              <th>Seed</th>
              <th>Registered</th>
            </tr>
          </thead>
          <tbody>
            {list.runs.map((run: RegistryRun) => (
              <tr key={run.database} data-testid="run-row">
                <td>
                  <input
                    type="checkbox"
                    aria-label={`pick ${run.database}`}
                    checked={picked.includes(run.database)}
                    onChange={() => pick(run.database)}
                  />
                </td>
                <td>
                  <a href={href({ page: "run", database: run.database })}>{run.database}</a>
                </td>
                <td>
                  <code>{run.run_id}</code>
                </td>
                <td>{run.scenario_id}</td>
                <td>{run.config}</td>
                <td>{run.contract}</td>
                <td>{run.seed}</td>
                <td>{when(run.created_wall_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}

export function RunList() {
  const load = useLoad("runs", loadRuns);
  return (
    <>
      <h1>Runs</h1>
      <Loaded load={load}>{(list) => <Runs list={list} />}</Loaded>
    </>
  );
}
