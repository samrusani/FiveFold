import type { Ledger as LedgerData } from "../types";
import { overdrawn } from "../views";
import { scalar } from "../describe";

export function Ledger({ ledger }: { ledger: LedgerData }) {
  return (
    <section className="panel" aria-label="Ledger">
      <h2>Ledger and fact history</h2>
      <h3>Effects</h3>
      {ledger.effects.length === 0 ? (
        <p className="muted">No effect committed.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th scope="col">Effect</th>
              <th scope="col">Intent</th>
              <th scope="col">Committed at vt</th>
              <th scope="col">Approval</th>
              <th scope="col">Basis</th>
              <th scope="col">Idempotency key</th>
              <th scope="col">Arguments</th>
            </tr>
          </thead>
          <tbody>
            {ledger.effects.map((effect) => (
              <tr key={effect.effect_id} data-testid="effect-row">
                <td>
                  <code>{effect.effect_id}</code>
                </td>
                <td>{effect.intent_id}</td>
                <td>{effect.committed_vt}</td>
                <td>{effect.approval_id}</td>
                <td>
                  {Object.entries(effect.basis)
                    .map(([key, version]) => `${key} v${scalar(version)}`)
                    .join(", ")}
                </td>
                <td>{effect.idempotency_key ?? <span className="muted">none</span>}</td>
                <td>
                  {Object.entries(effect.args)
                    .map(([key, value]) => `${key} ${scalar(value)}`)
                    .join(", ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h3>Facts</h3>
      <table>
        <thead>
          <tr>
            <th scope="col">Fact</th>
            <th scope="col">Version</th>
            <th scope="col">From vt</th>
            <th scope="col">Cause</th>
            <th scope="col">Value</th>
          </tr>
        </thead>
        <tbody>
          {ledger.facts.map((fact) => (
            <tr key={`${fact.key}-${fact.version}`} className={overdrawn(fact) ? "overdrawn" : ""}>
              <td>
                <code>{fact.key}</code>
              </td>
              <td>{fact.version}</td>
              <td>{fact.valid_from_vt}</td>
              <td>
                {fact.cause}
                {fact.cause_effect_id === null ? "" : ` ${fact.cause_effect_id}`}
                {fact.cause_actor === null ? "" : ` ${fact.cause_actor}`}
              </td>
              <td>
                {Object.entries(fact.value)
                  .map(([key, value]) => `${key} ${scalar(value)}`)
                  .join(", ")}
                {overdrawn(fact) ? <strong className="warn"> over the limit</strong> : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
