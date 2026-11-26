import type { Scorecard as Card } from "../types";
import { Verdict } from "./common";

const PROPERTY_TITLES: Record<number, string> = {
  1: "Exactly once",
  2: "Authorized at commit",
  3: "Inputs still fresh",
  4: "Actually committed",
  5: "Truthfully reported",
};

export function Scorecard({ card }: { card: Card | null }) {
  if (card === null) {
    return (
      <section className="panel" aria-label="Scorecard">
        <h2>Scorecard</h2>
        <p className="muted">The evaluator has not scored this run yet.</p>
      </section>
    );
  }
  return (
    <section className="panel" aria-label="Scorecard">
      <h2>
        Scorecard <Verdict verdict={card.verdict} />
      </h2>
      <table className="scorecard">
        <thead>
          <tr>
            <th scope="col">Property</th>
            <th scope="col">Verdict</th>
            <th scope="col">Reason codes</th>
            <th scope="col">Failed checks</th>
          </tr>
        </thead>
        <tbody>
          {card.properties.map((prop) => (
            <tr key={prop.property} data-testid="property-row">
              <td>
                {prop.property}. {PROPERTY_TITLES[prop.property] ?? prop.name}
              </td>
              <td>
                <Verdict verdict={prop.verdict} />
              </td>
              <td>
                {prop.reason_codes.length === 0 ? (
                  <span className="muted">none</span>
                ) : (
                  prop.reason_codes.map((code) => (
                    <code key={code} className="reason">
                      {code}
                    </code>
                  ))
                )}
              </td>
              <td>
                <ul className="failures">
                  {prop.failures.map((failure, index) => (
                    <li key={index}>
                      <code>{failure.code}</code>
                      {failure.intent_id === null ? "" : ` ${failure.intent_id}`}
                      {failure.effect_ids.length === 0 ? "" : ` (${failure.effect_ids.join(", ")})`}
                      : {failure.detail}
                    </li>
                  ))}
                </ul>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3>Excused intents</h3>
      {card.excused_intents.length === 0 ? (
        <p className="muted">None: property 4 excused no intent.</p>
      ) : (
        <ul className="excused">
          {card.excused_intents.map((intent) => (
            <li key={intent.intent_id} data-testid="excused-intent">
              <strong>{intent.intent_id}</strong> <code>{intent.excuse}</code>
              {intent.approval_ids.length === 0 ? "" : ` (${intent.approval_ids.join(", ")})`}:{" "}
              {intent.detail}
            </li>
          ))}
        </ul>
      )}
      <h3>The report's claims</h3>
      {card.report === null ? (
        <p className="muted">No valid report counted.</p>
      ) : (
        <table className="claims">
          <thead>
            <tr>
              <th scope="col">Intent</th>
              <th scope="col">Claim</th>
              <th scope="col">What the stack knew</th>
              <th scope="col">Ledger</th>
              <th scope="col">Judged</th>
            </tr>
          </thead>
          <tbody>
            {card.report.claims.map((claim) => (
              <tr key={claim.intent_id}>
                <td>{claim.intent_id}</td>
                <td>
                  {claim.status} {claim.claimed_effects.join(" ")}
                </td>
                <td>
                  {claim.knowledge} {claim.known_effects.join(" ")}
                </td>
                <td>{claim.ledger_effects.join(" ") || "no effect"}</td>
                <td>
                  {claim.passed ? "holds" : "fails"}
                  {claim.reason_code === null ? "" : ` (${claim.reason_code})`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
