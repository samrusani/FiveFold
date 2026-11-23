# Shrink a failing scripted run

Run shrinking finds a smaller fault plan that reproduces a scored failure in
an in-process scripted B0, G1 or G2 run. It keeps the verdict, the failing
properties and the same pairs of reason code and intent for each property.
The scenario, configuration, contract, seed and initial workers stay fixed.
[Record 0022](adr/0022-run-shrinking.md)

## From a study result to a smaller plan

From a checkout with Python 3.12 and PostgreSQL 16, start the database and
run the F5 beyond-deadline cell for B0 at C2. `make db-up` starts the Compose
database. The commands use `FIVEFOLD_ADMIN_DSN` if you supply another
PostgreSQL server. The study and shrink output folders both need unused names.

```sh
make db-up
uv run python -m fivefold.harness.study --configs B0 --contracts C2 \
  --cells F5-beyond_deadline --seed 7 --out shrink-source
uv run python -m fivefold.shrink \
  --result shrink-source/F5-beyond_deadline-C2-B0-seed-7.json \
  --scenario scenarios/dev/dev-001-single-po.yaml --out shrunk
```

The study reports `fail (1, 2, 3)`. The original plan holds the first order
for 20,000 ms. The shrinker tries two candidates and writes this plan to
`shrunk/faults.yaml`.

```yaml
- fault: F5
  variant: beyond_deadline
  at:
    point: sim.write.received
    match:
      api: submit_purchase_order
    occurrence: 1
  params:
    delay_ms: 10001
```

The new delay is 10,001 ms, one millisecond past the contract's commit
deadline in this scenario. The same three properties fail for the same
reasons and intent. A whole fault entry can also disappear when the remaining
plan reproduces the same failure.

## Inputs, limits and evidence

- Use `--database NAME` in place of `--result FILE` to read a registered run.
  In either case supply the exact original scenario and the run's complete
  scored result. The shrinker uses the materialized plan, including any fault
  parameters drawn from a seed, and first checks that the original plan
  still reproduces the failure.
- Each candidate runs in a fresh database. The shrinker removes only the
  databases it creates. Use `--keep-database` to keep the final run in the
  [viewer](viewer.md).
- The default search budget is 100 distinct candidates. Change it with
  `--max-candidates N`. Set `--wall-time-s SECONDS` to stop between candidates
  when elapsed time reaches that limit. To repeat a timed stop, use the
  `candidate_count` in `shrink.json` as the candidate budget.
- `result.json` holds the final score. `records.json` holds the final run's
  playback evidence with wall-clock fields removed. `shrink.json` gives the
  original and final signatures, plan digests, candidates, elapsed time and
  `budget_exhausted` status. A budget stop still returns a verified plan and
  records the next candidate it would have tried.
- Use [ordinary playback](tour.md) for cassettes from live, replayed or
  branched runs. B1, B2 and split runs require separate verification before
  shrinking can support them.
