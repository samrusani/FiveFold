import { useState } from "react";

import { describe, recordOf } from "../describe";
import type { TimelineEvent } from "../types";
import { filterByHops, hopsOf } from "../views";
import { Json } from "./common";

const SHARED_VT_NOTE =
  "Another event has the same virtual time. Harness events are listed before gateway events " +
  "and each source keeps its own order, but which happened first is not known (record 0012).";

export function Timeline({ events }: { events: TimelineEvent[] }) {
  const hops = hopsOf(events);
  const [hidden, setHidden] = useState<ReadonlySet<string>>(new Set());
  const shown = filterByHops(events, new Set(hops.filter((hop) => !hidden.has(hop))));
  const toggle = (hop: string) =>
    setHidden((before) => {
      const after = new Set(before);
      if (after.has(hop)) after.delete(hop);
      else after.add(hop);
      return after;
    });
  return (
    <section className="panel" aria-label="Timeline">
      <h2>Timeline</h2>
      <p className="muted">
        Every message with its fate, the faults that fired, the scripted humans' actions, the
        reports and the gateway's action events, in virtual time. A{" "}
        <span className="tie">=</span> marks events that share a virtual time.
      </p>
      <fieldset className="hops">
        <legend>Messages on hops</legend>
        {hops.map((hop) => (
          <label key={hop}>
            <input type="checkbox" checked={!hidden.has(hop)} onChange={() => toggle(hop)} /> {hop}
          </label>
        ))}
        <span className="muted">
          {shown.length} of {events.length} events
        </span>
      </fieldset>
      <table className="timeline">
        <thead>
          <tr>
            <th scope="col">vt (ms)</th>
            <th scope="col">Source</th>
            <th scope="col">Event</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((event) => {
            const text = describe(event);
            return (
              <tr
                key={`${event.source}-${event.seq}`}
                className={`tone-${text.tone}`}
                data-testid="timeline-row"
                data-kind={event.kind}
              >
                <td className="vt">
                  {event.vt}
                  {event.shares_vt ? (
                    <span className="tie" title={SHARED_VT_NOTE} role="img" aria-label="shares its virtual time">
                      =
                    </span>
                  ) : null}
                </td>
                <td className="source">
                  {event.source} #{event.seq}
                </td>
                <td>
                  <details>
                    <summary>
                      <span className="title">{text.title}</span>
                      {text.detail ? <span className="detail"> · {text.detail}</span> : null}
                    </summary>
                    <Json value={recordOf(event)} />
                  </details>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
