# Viewer

The viewer shows the runs on your PostgreSQL server: each run's timeline and five-property scorecard, and a demo's B0 and G2 runs side by side (record 0012). It only reads. It needs Node 26, the current LTS line, and npm, to build its frontend once.

An installed package has no built frontend. Its viewer shows a not-built page and still serves the API. To use the frontend with an installed package, build it from a checkout and pass its `viewer/dist` directory with `python -m fivefold.viewer --static PATH`.

```
make viewer-build   # install the viewer's locked npm packages and build its frontend
make demo           # run the demos; each run lists itself in the run registry
make viewer         # serve the viewer on http://127.0.0.1:8300
```

What it shows:

- **Runs:** every run database the run registry lists, newest first. Suggested B0 and G2 pairs share a scenario and its digest, contract level, seed, topology, model adapter, model and run parameters. Their planned faults must match by ID and variant in order. The viewer reads these fields from each run. It recovers the catalog's F7 timing and F4/F12 hop variants from saved plans. F4 and F12 have distinct variants for each hop. Demo 1 still pairs because F2 has no variant and its two injection points describe the same fault. Any two runs can be compared manually. An experimental branch shows as a new experiment of its parent, at its branch point, and is never paired (record 0009).
- **A run:** its scorecard, from the evaluator: each property's verdict, reason codes and failed checks, the intents property 4 excuses, and how each claim of the report was judged. Its timeline: every message on every hop with its fate, the faults that fired, the scripted humans' actions, the reports and, in G1 and G2, the gateway's action events. Events go by virtual time; at one virtual time, harness events come before gateway events, and `=` marks every event that shares its virtual time with another, because which came first is then not known. Messages can be filtered by hop. Last, the effects ledger and the fact history.
- **Side by side:** two runs aligned by virtual time, each with the agent's view (what the agent sent and was sent, the faults that hit it, its final report) beside its ledger (each effect and fact change). In demo 1, B0's resumed agent orders again and the budget goes over its limit; G2's resumed agent gets the order the gateway already confirmed.

The run registry is a small database, `fivefold_registry` (set `FIVEFOLD_REGISTRY_DB` for another), with one row per run database. Every run writes its row as it starts: demos, studies, recorded playback, split runs and tests. A study's dropped episode database leaves the registry. `python -m fivefold.harness.migrations bootstrap` creates the registry; the demo, study and recorder commands and the split topology's `start` do so too.

The viewer connects to the server `FIVEFOLD_ADMIN_DSN` names, always as `ff_viewer`, which can only select, in read-only transactions. It opens only databases the registry lists, and reads only their `sim`, `gw`, `harness` and `eval` schemas. Its API answers JSON to `GET` only: `/api/runs`, `/api/runs/DATABASE` (the run and its scorecard), `/api/runs/DATABASE/timeline` and `/api/runs/DATABASE/ledger`. A run is named by its database, since a recording and its replay share a run id.

With Docker Compose, `docker compose up viewer` builds the frontend and the API into one image and serves the viewer on 127.0.0.1:8300 against the Compose database.

To work on the frontend, `cd viewer`, then `npm run dev` serves it with hot reload on port 5173 and passes `/api` to a viewer on port 8300; `npm test` runs the Vitest tests, `npm run lint` runs ESLint, and `npm run build` type checks and builds. `make viewer-e2e` checks a running viewer in Chromium against a demo's runs and saves a screenshot of the side-by-side page; it needs Playwright's Chromium (`npx playwright install chromium`, once).
