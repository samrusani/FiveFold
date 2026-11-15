import { useState } from "react";

import { describe, describeLedger } from "../describe";
import { useLoad } from "../load";
import { href } from "../route";
import type { TimelineEvent } from "../types";
import type { LedgerEntry } from "../views";
import { agentView, alignByVt, ledgerEntries, overdrawn } from "../views";
import { Loaded, Verdict } from "./common";
import { loadPair, pairKey, type RunData } from "../runData";

function Heading({ data }: { data: RunData }) {
  const { run, scorecard } = data.summary;
  const failing = (scorecard?.properties ?? []).filter((prop) => prop.verdict === "fail");
  const faults = data.timeline.events.flatMap((e) =>
    e.kind === "fault" ? [`${e.fault.fault} at ${e.fault.point} (vt ${e.vt})`] : [],
  );
  return (
    <th colSpan={2} scope="colgroup">
      <a href={href({ page: "run", database: data.summary.database })}>
        {run.config} · {run.contract} · seed {run.seed}
      </a>{" "}
      <Verdict verdict={scorecard?.verdict ?? null} />
      <div className="muted small">
        {failing.length === 0
          ? "every property holds"
          : failing.map((prop) => `${prop.property} fails (${prop.reason_codes.join(", ")})`).join("; ")}
      </div>
      <div className="muted small">faults: {faults.join("; ") || "none"}</div>
    </th>
  );
}

function AgentCell({ events }: { events: TimelineEvent[] }) {
  return (
    <td className="agent">
      {events.map((event) => {
        const text = describe(event);
        return (
          <div key={`${event.source}-${event.seq}`} className={`item tone-${text.tone}`} title={text.detail}>
            <span className="title">{text.title}</span>
            {text.detail ? <span className="detail">{text.detail}</span> : null}
          </div>
        );
      })}
    </td>
  );
}

function LedgerCell({ entries }: { entries: LedgerEntry[] }) {
  return (
    <td className="ledger">
      {entries.map((entry) => {
        const text = describeLedger(entry);
        const warn = entry.kind === "fact" && overdrawn(entry.fact);
        return (
          <div key={`${entry.kind}-${entry.seq}-${text.title}`} className={`item${warn ? " overdrawn" : ""}`}>
            <span className="title">{text.title}</span>
            <span className="detail">
              {text.detail}
              {warn ? <strong className="warn"> over the limit</strong> : null}
            </span>
          </div>
        );
      })}
    </td>
  );
}

function SideBySide({ runs }: { runs: [RunData, RunData] }) {
  const [handshakes, setHandshakes] = useState(false);
  const columns = runs.flatMap((data) => [
    { key: "agent", items: agentView(data.timeline.events, { handshakes }), vt: (e: unknown) => (e as TimelineEvent).vt },
    { key: "ledger", items: ledgerEntries(data.ledger), vt: (e: unknown) => (e as LedgerEntry).vt },
  ]);
  const rows = alignByVt(columns);
  return (
    <>
      <p className="muted">
        Each run's agent's view (what the agent sent and was sent, the faults that hit it, and
        its report) beside its ledger (effects and fact changes), aligned by virtual time.{" "}
        <label>
          <input type="checkbox" checked={handshakes} onChange={() => setHandshakes(!handshakes)} /> show
          MCP handshakes
        </label>
      </p>
      <table className="compare">
        <thead>
          <tr>
            <th rowSpan={2}>vt (ms)</th>
            <Heading data={runs[0]} />
            <Heading data={runs[1]} />
          </tr>
          <tr>
            <th>Agent's view</th>
            <th>Ledger</th>
            <th>Agent's view</th>
            <th>Ledger</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.vt} data-testid="compare-row">
              <td className="vt">{row.vt}</td>
              <AgentCell events={row.cells[0] as TimelineEvent[]} />
              <LedgerCell entries={row.cells[1] as LedgerEntry[]} />
              <AgentCell events={row.cells[2] as TimelineEvent[]} />
              <LedgerCell entries={row.cells[3] as LedgerEntry[]} />
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

export function Compare({ left, right }: { left: string; right: string }) {
  const load = useLoad(pairKey(left, right), loadPair);
  return (
    <>
      <h1>Side by side</h1>
      <Loaded load={load}>{(runs) => <SideBySide runs={runs} />}</Loaded>
    </>
  );
}
