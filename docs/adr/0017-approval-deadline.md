# 0017: End of the approval wait

Status: Proposed  
Date: 2026-11-18  
Spec sections affected: approval and grant lifecycle, PendingApproval, property 4

## Context

Record 0002, rule 14, describes a timeout when no decision arrives within
`approval_wait_ms`. Record 0010 requires a last poll and acts on an approval
decided just before the deadline. The poll can also see an approval decided
after the deadline. Audit finding 3 concerns that case.

The scripted `dev-001-single-po` scenario, G2, C2, seed 7, reproduced it
with an empty fault plan. This run required approval for every quote
(`auto_approve_up_to_minor: 0`), used a 1,000 ms approval wait, and scripted
approval 1,030 ms after the simulator received the request. Times are virtual
milliseconds since the run started.

| Event | Time (ms) |
| --- | ---: |
| Simulator received the approval request | 4,648 |
| Gateway entered PendingApproval (T2) | 4,670 |
| Gateway's wait deadline and final poll sent | 5,670 |
| Authority approved | 5,678 |
| Simulator received the final poll | 5,717 |
| Gateway received the final poll's approved answer | 5,763 |
| Gateway recorded approval (T4) | 5,856 |
| Gateway dispatched the order | 6,050 |
| Simulator committed the order | 6,055 |

The decision was 8 ms after the deadline. The approval was valid at commit.
The run passed properties 2 and 4.

## Decision

Recommend A. Keep waiting for the result of the first poll completed at or
after the deadline. Time out only if that result still reads pending. Apply
the existing decision and freshness checks to an answer with a decision.
This makes the documented rule match the observed behavior.

## Alternatives considered

### A. Keep the final poll rule

Keep T4 (`approved`, PendingApproval to Ready) and T5
(`approval_timeout` and the other existing rejection events) as they are.
The grants keep their current transitions. The simulator still checks the
approval at commit for property 2. A late approval can enable an acceptable
effect under property 4, as in the reproduction. There are no changes to
`faults/expected.yaml` because the implementation stays as it is.

### B. Make the authority's decision time the cutoff

Carry the simulator's `decided_vt` through the gateway's approval view.
For a positive approval decided strictly after the deadline, use T5
(`approval_timeout`, PendingApproval to Rejected) and mark the gateway grant
timed out. Keep the existing T4 checks for on-time approvals. The
prototype took T5 at 5,763 ms, left the simulator's approval valid and unused,
and made zero order commits. Property 2 then held with zero effects to
check. Property 4 failed because the unused authority was still valid
and did not qualify for the `authority_withdrawn` excuse in record 0004.

`tests/pg/test_expected.py` passed all 240 cases against the local
prototype, including the checks of the 235 expected-outcome rows. No rows
in `faults/expected.yaml` changed. The separate reproduction above checks
the timing case that the suite does not cover.

## Consequences

Under A, a decision after the deadline can still be used if the final poll
sees it. The interval depends on delivery of the poll and answer. Under B,
the gateway enforces a strict decision time while a valid simulator approval
can go unused. Withdrawing or expiring such authority would need a separate
decision about the grant lifecycle and property 4.

## Spec changes on acceptance

Update rule 14 of record 0002 and the corresponding wait rule in the
specification to describe A's final poll. Keep T4, T5 and the properties
as defined in records 0003 and 0004. Add a regression for the observed
late approval when implementing the accepted decision.
