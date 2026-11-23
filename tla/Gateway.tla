---- MODULE Gateway ----
EXTENDS Naturals, FiniteSets, Sequences, GatewayTables

CONSTANTS Slots, MaxAttempts, MaxProbes, MaxCrashes

\* One G2 intent, with two distinguishable normalized argument forms.
Args == {"A", "B"}
VARIABLES c, up, crashes, closed, actions
vars == <<c, up, crashes, closed, actions>>
Ids == 1..Slots
Undispatched == {"proposed", "pending_approval", "authorized"}
Unknown == "outcome_unknown"
Holding == Undispatched \cup {"dispatched", Unknown, "confirmed"}
Clean == [state |-> "none", args |-> "none", grant |-> "none", auth |-> "valid", basis |-> "fresh",
          attempts |-> <<>>, journal |-> <<>>, effect |-> 0, known |-> {}, ledger |-> 0,
          escalated |-> FALSE, probes |-> 0, settled |-> FALSE,
          wire |-> "idle", pending |-> FALSE, copied |-> FALSE, seen |-> [event |-> 0, state |-> "none"],
          report |-> [claim |-> "none", event |-> 0, known |-> {}]]
Init ==
  /\ c \in {"C0", "C1", "C2", "C3"}
  /\ up = TRUE /\ crashes = 0 /\ closed = FALSE
  /\ actions = [i \in Ids |-> Clean]

Tx(r, tid, to, why, grant, effect, known) ==
  [r EXCEPT !.state = to, !.grant = grant, !.effect = effect, !.known = known,
     !.journal = Append(@, [edge |-> <<tid, r.state, to, why>>,
                            grants |-> <<r.grant, grant>>, effect |-> effect, known |-> known])]

Replace(i, r) == /\ actions' = [actions EXCEPT ![i] = r]
                 /\ UNCHANGED <<c, up, crashes, closed>>

\* A proposal at the intent row returns a disposition without mutating the holder.
Holders == {i \in Ids : actions[i].state \in Holding}
Offer(args) ==
  IF Holders = {} THEN [kind |-> "create", holder |-> 0]
  ELSE LET i == CHOOSE j \in Holders : TRUE
           r == actions[i]
           kind == IF r.args = args THEN "attach"
                   ELSE IF r.state = "confirmed" THEN "intent_done" ELSE "intent_busy"
       IN [kind |-> kind, holder |-> i]

\* The extra action explores a representative reopening: a denied approval
\* with no earlier read or crash. The first slot still explores all faults.
Reopened ==
  /\ crashes = 0 /\ actions[1].seen.event = 0
  /\ actions[1].auth = "valid" /\ actions[1].basis = "fresh"
  /\ Len(actions[1].journal) = 3
  /\ actions[1].journal[3].edge[4] = "approval_denied"

\* With no holder the names of the two forms are interchangeable. Use A for
\* the first slot. The second slot can reuse A or choose B.
Admit(i, r) ==
  /\ up /\ ~closed /\ r.state = "none"
  /\ i = 1 \/ Reopened
  /\ \E args \in (IF i = 1 THEN {"A"} ELSE Args):
       /\ Offer(args).kind = "create"
       /\ Replace(i, [Tx(r,"T1","proposed","admitted",r.grant,0,{}) EXCEPT !.args = args])

RequestApproval(i, r) ==
  /\ up /\ r.state = "proposed"
  /\ IF r.basis = "fresh"
     THEN Replace(i, Tx(r,"T2","pending_approval","approval_requested","pending",0,{}))
     ELSE Replace(i, Tx(r,"T3","rejected","stale_input",r.grant,0,{}))

DecideApproval(i, r) ==
  /\ up /\ r.state = "pending_approval"
  /\ \/ /\ r.auth = "valid" /\ r.basis = "fresh"
        /\ Replace(i, Tx(r,"T4","authorized","approved","active",0,{}))
     \/ \E pair \in {<<"approval_denied","denied">>,<<"approval_timeout","timed_out">>}:
          Replace(i, Tx(r,"T5","rejected",pair[1],pair[2],0,{}))
     \/ /\ r.auth = "revoked"
        /\ Replace(i, Tx(r,"T5","rejected","approval_revoked","revoked",0,{}))
     \/ /\ r.auth = "expired"
        /\ Replace(i, Tx(r,"T5","rejected","approval_expired","expired",0,{}))
     \/ /\ r.auth = "valid" /\ r.basis # "fresh"
        /\ Replace(i, Tx(r,"T5","rejected","stale_input","released",0,{}))

Needed(n) ==
  LET attemptClass == IF n = 1 THEN 1 ELSE 2
      checks == {row \in SendChecks : row[1] = c /\ row[2] = attemptClass}
  IN IF checks = {} THEN "forbidden" ELSE (CHOOSE row \in checks : TRUE)[3]
FirstCheck == Needed(1)
Passes(auth, basis, kind) ==
  kind = "exempt" \/ (kind # "forbidden" /\ auth = "valid" /\
    (basis = "fresh" \/ (kind = "exact_only" /\ basis = "sufficient_stale")))

LogAttempt(i, r, first) ==
  LET n == Len(r.attempts)+1
      step == IF first THEN "T6" ELSE "T13"
      reason == IF first THEN "dispatched" ELSE "retry"
      grant == IF first THEN "consumed" ELSE r.grant
      key == IF c \in {"C2","C3"} THEN i ELSE 0
      row == [grant |-> i, key |-> key, kind |-> Needed(n), auth |-> r.auth,
              basis |-> r.basis,event |-> Len(r.journal)+1]
      next == Tx(r,step,"dispatched",reason,grant,r.effect,r.known)
  IN /\ up /\ Passes(r.auth,r.basis,Needed(n))
     /\ Replace(i, [next EXCEPT !.attempts = Append(@, row), !.wire = "outbox",
                                 !.settled = FALSE])

Dispatch(i,r) ==
  /\ up /\ r.state = "authorized"
  /\ IF Passes(r.auth,r.basis,FirstCheck)
     THEN LogAttempt(i,r,TRUE)
     ELSE LET why == IF r.auth = "valid" THEN "stale_input"
                     ELSE IF r.auth = "revoked" THEN "approval_revoked" ELSE "approval_expired"
              grant == IF r.auth = "valid" THEN "released" ELSE r.auth
          IN Replace(i, Tx(r,"T7","rejected",why,grant,0,{}))

Send(i,r) == /\ up /\ r.state = "dispatched" /\ r.wire = "outbox"
             /\ Replace(i,[r EXCEPT !.wire = "sent"])
Deliver(i,r) == /\ r.wire = "sent" /\ ~r.pending
                /\ Replace(i,[r EXCEPT !.wire = "waiting", !.pending = TRUE])
Drop(i,r) == /\ r.wire = "sent" /\ Replace(i,[r EXCEPT !.wire = "silent"])
Duplicate(i,r) == /\ r.wire = "committed" /\ ~r.copied /\ ~r.pending
                  /\ Replace(i,[r EXCEPT !.copied = TRUE, !.pending = TRUE])
Commit(i,r) ==
  /\ r.pending
  /\ c # "C3" \/ r.ledger > 0 \/ (r.auth = "valid" /\ r.basis = "fresh")
  /\ LET copies == IF r.ledger = 0 THEN 1
                   ELSE IF c \in {"C2","C3"} THEN r.ledger ELSE 2
         reply == IF up /\ r.state = "dispatched" THEN "committed" ELSE "silent"
      IN Replace(i,[r EXCEPT !.ledger = copies, !.pending = FALSE, !.wire = reply])
Reject(i,r) ==
  /\ r.pending /\ r.ledger = 0
  /\ LET definitive == IF c = "C3" /\ (r.auth # "valid" \/ r.basis # "fresh")
                         THEN "precondition" ELSE "rejected"
     IN Replace(i,[r EXCEPT !.pending = FALSE,
          !.wire = IF up /\ r.state = "dispatched" THEN definitive ELSE "silent"])
LostReply(i,r) == /\ r.wire \in {"committed","rejected","precondition"}
                  /\ Replace(i,[r EXCEPT !.wire = "silent"])
\* Each ledger slot has a different effect ID. Slot numbers are local to
\* the action. C2 and C3 always reuse the first slot through the stable key.
EffectId(i, n) == 2 * (i - 1) + n
Effects(i, r) == {EffectId(i, n) : n \in 1..r.ledger}

Answer(i,r) ==
  /\ up /\ r.state = "dispatched"
  /\ \/ /\ r.wire = "committed"
        /\ \E id \in Effects(i, r): Replace(i, Tx(r,"T8","confirmed","committed",r.grant,id,{id}))
     \/ /\ r.wire \in {"rejected","precondition"}
        /\ LET why == IF r.wire = "precondition" THEN "precondition_failed" ELSE "destination_rejected"
            IN Replace(i, Tx(r,"T9","failed",why,r.grant,0,{}))
InProgress(i,r) ==
  /\ up /\ c \in {"C2","C3"} /\ r.state = "dispatched" /\ Len(r.attempts)>1
  /\ r.pending /\ r.wire = "sent"
  /\ Replace(i,[Tx(r,"T10",Unknown,"in_progress",r.grant,0,{}) EXCEPT !.wire = "silent"])
Timeout(i,r) ==
  /\ up /\ r.state = "dispatched" /\ r.wire \in {"sent","waiting","silent"}
  /\ Replace(i,[Tx(r,"T10",Unknown,"timeout",r.grant,0,{}) EXCEPT !.wire = "silent"])

Crash == /\ up /\ crashes < MaxCrashes /\ ~closed
         /\ \E i \in Ids : actions[i].state \in Undispatched \cup {"dispatched"}
         /\ up' = FALSE /\ crashes' = crashes + 1 /\ UNCHANGED <<c,closed,actions>>
Restart ==
  /\ ~up /\ up' = TRUE /\ UNCHANGED <<c,crashes,closed>>
  /\ actions' = [i \in Ids |-> LET r == actions[i] IN
       IF r.state = "dispatched" THEN
         [Tx(r,"T10",Unknown,"recovered_open_attempt",r.grant,0,{}) EXCEPT !.wire = "silent"]
       ELSE r]

Settle(i,r) == /\ r.state = Unknown /\ ~r.settled
               /\ Replace(i,[r EXCEPT !.settled = TRUE])
Probe(i,r) ==
  /\ up /\ c = "C1" /\ r.state = Unknown /\ ~r.escalated /\ r.probes < MaxProbes
  /\ \/ \E id \in Effects(i, r):
          \E listed \in {{id}, Effects(i, r)}:
            Replace(i,Tx(r,"T11","confirmed","status_found",r.grant,id,listed))
     \/ /\ r.settled /\ Replace(i,Tx(r,"T12","failed","status_settled_empty",r.grant,0,{}))
     \/ Replace(i,[r EXCEPT !.probes = @+1])
Retry(i,r) ==
  /\ r.state = Unknown /\ ~r.escalated /\ c \in {"C2","C3"} /\ Len(r.attempts) < MaxAttempts
  /\ LogAttempt(i,r,FALSE)
Escalate(i,r) ==
  LET failedCheck == c = "C2" /\ Len(r.attempts) < MaxAttempts
                      /\ ~Passes(r.auth,r.basis,Needed(2))
      grant == IF r.auth = "valid" THEN r.grant ELSE r.auth
      escalated == IF failedCheck
                   THEN Tx(r,"note",r.state,"retry_check_failed",grant,r.effect,r.known)
                   ELSE r
  IN /\ up /\ r.state = Unknown /\ ~r.escalated
     /\ \/ c = "C0"
        \/ c = "C1" /\ r.probes = MaxProbes
        \/ c \in {"C2","C3"} /\ Len(r.attempts) = MaxAttempts
        \/ failedCheck
     /\ Replace(i,[escalated EXCEPT !.escalated = TRUE])
Attest(i,r) ==
  /\ up /\ r.state = Unknown /\ r.escalated
  /\ \/ \E id \in Effects(i, r): Replace(i,Tx(r,"T11","confirmed","human_attested",r.grant,id,{id}))
     \/ /\ r.settled /\ Replace(i,Tx(r,"T12","failed","human_attested",r.grant,0,{}))
Authority(i,r) ==
  /\ r.auth = "valid" /\ r.state \in {"pending_approval","authorized","dispatched",Unknown}
  /\ \E auth \in {"revoked","expired"} : Replace(i,[r EXCEPT !.auth = auth])
Quote(i,r) ==
  /\ r.basis = "fresh" /\ r.state \in Undispatched \cup {"dispatched",Unknown}
  /\ \E basis \in {"exact_stale","sufficient_stale"} : Replace(i,[r EXCEPT !.basis = basis])

Closure(r) ==
  CASE r.state = "proposed" -> Tx(r,"T3","rejected","run_closed",r.grant,0,{})
    [] r.state = "pending_approval" -> Tx(r,"T5","rejected","run_closed","released",0,{})
    [] r.state = "authorized" -> Tx(r,"T7","rejected","run_closed","released",0,{})
    [] OTHER -> r
Close ==
  /\ up /\ ~closed /\ closed' = TRUE
  /\ actions' = [i \in Ids |-> Closure(actions[i])]
  /\ UNCHANGED <<c,up,crashes>>
Read(i,r) ==
  /\ up /\ r.state # "none" /\ r.seen.event = 0
  /\ Replace(i,[r EXCEPT !.seen = [event |-> Len(r.journal), state |-> r.state]])
Report(i,r) ==
  /\ up /\ ~closed /\ r.report.event = 0 /\ r.state # "none"
  /\ \E claim \in {"done", "not_done", "unknown"}:
     /\ \/ claim = "done" /\ r.state = "confirmed" /\ r.known # {}
        \/ claim = "not_done" /\ r.state \in Undispatched \cup {"rejected","failed"}
        \/ claim = "unknown" /\ r.state \in {"dispatched",Unknown}
     /\ LET audited == [r EXCEPT !.report = [claim |-> claim, event |-> Len(r.journal),
                    known |-> IF claim = "done" THEN r.known ELSE {}]]
         IN actions' = [j \in Ids |-> Closure(IF i = j THEN audited ELSE actions[j])]
     /\ closed' = TRUE /\ UNCHANGED <<c,up,crashes>>

Halted == /\ closed /\ up
          /\ \A i \in Ids : LET r == actions[i] IN
               r.state \in {"none","confirmed","rejected","failed"} \/ (r.state = Unknown /\ r.escalated)
          /\ UNCHANGED vars
Next == Crash \/ Restart \/ Close \/ Halted \/
  \E i \in Ids : LET r == actions[i] IN
    \/ Admit(i,r) \/ RequestApproval(i,r) \/ DecideApproval(i,r) \/ Dispatch(i,r)
    \/ Send(i,r) \/ Deliver(i,r) \/ Drop(i,r) \/ Duplicate(i,r) \/ Commit(i,r) \/ Reject(i,r)
    \/ LostReply(i,r) \/ Answer(i,r) \/ InProgress(i,r) \/ Timeout(i,r)
    \/ Settle(i,r) \/ Probe(i,r) \/ Retry(i,r) \/ Escalate(i,r) \/ Attest(i,r)
    \/ Authority(i,r) \/ Quote(i,r) \/ Read(i,r) \/ Report(i,r)
Spec == Init /\ [][Next]_vars

I1 == \A i \in Ids : LET r == actions[i] IN
  /\ r.effect \in {0} \cup Effects(i, r)
  /\ r.effect = (IF Len(r.journal) = 0 THEN 0 ELSE r.journal[Len(r.journal)].effect)
  /\ \A k \in 1..Len(r.journal) : LET row == r.journal[k] IN
       /\ row.effect \in {0} \cup Effects(i, r)
       /\ IF k > 1 /\ r.journal[k-1].effect # 0 THEN row.effect = r.journal[k-1].effect
          ELSE row.effect = 0 \/ row.edge[1] \in {"T8","T11"}
I2 == \A i \in Ids : LET r == actions[i] IN
  /\ \A k \in 1..Len(r.attempts) : r.attempts[k].grant = i
  /\ r.grant = (IF Len(r.journal) = 0 THEN "none" ELSE r.journal[Len(r.journal)].grants[2])
  /\ \A k \in 1..Len(r.journal) : LET grants == r.journal[k].grants IN
         /\ grants[1] = (IF k = 1 THEN "none" ELSE r.journal[k-1].grants[2])
         /\ grants[1] = grants[2] \/ grants \in GrantEdges
  /\ Cardinality({k \in 1..Len(r.journal) : r.journal[k].grants = <<"active","consumed">> }) <= 1
I3 == \A i \in Ids : LET r == actions[i] IN
  \A k \in 1..Len(r.attempts) : LET a == r.attempts[k] IN
    /\ a.kind = Needed(k) /\ Passes(a.auth,a.basis,a.kind)
    /\ r.journal[a.event].edge[1] = (IF k = 1 THEN "T6" ELSE "T13")
I4 == \A i \in Ids : LET r == actions[i] IN
  /\ (c \in {"C0","C1"} => Len(r.attempts) <= 1)
  /\ \A k \in 1..Len(r.attempts):
     r.attempts[k].key = (IF c \in {"C2","C3"} THEN i ELSE 0)
I5 == \A i \in Ids : LET r == actions[i] IN
  /\ IF r.seen.event = 0 THEN TRUE ELSE r.seen.state = r.journal[r.seen.event].edge[3]
  /\ IF r.report.event = 0 THEN TRUE ELSE LET e == r.journal[r.report.event] IN
     CASE r.report.claim = "done" -> e.edge[3] = "confirmed" /\ r.report.known = e.known
       [] r.report.claim = "not_done" -> e.edge[3] \in Undispatched \cup {"rejected","failed"}
       [] OTHER -> e.edge[3] \in {"dispatched",Unknown}
I6 == \A i \in Ids : LET r == actions[i] IN
  /\ r.state = (IF Len(r.journal) = 0 THEN "none" ELSE r.journal[Len(r.journal)].edge[3])
  /\ \A k \in 1..Len(r.journal): LET edge == r.journal[k].edge IN
         /\ edge[2] = (IF k = 1 THEN "none" ELSE r.journal[k-1].edge[3])
         /\ IF edge[1] = "note"
            THEN edge = <<"note",Unknown,Unknown,"retry_check_failed">>
            ELSE edge \in LegalEdges(c)
I7 ==
  /\ Cardinality(Holders) <= 1
  /\ \A args \in Args : LET result == Offer(args) IN
      /\ (Holders = {} => result.kind = "create")
      /\ \A i \in Holders:
           /\ result.holder = i
           /\ (actions[i].args = args => result.kind = "attach")
           /\ (actions[i].args # args /\ actions[i].state = "confirmed" => result.kind = "intent_done")
           /\ (actions[i].args # args /\ actions[i].state # "confirmed" => result.kind = "intent_busy")
AppendOnly == \A i \in Ids :
    SubSeq(actions'[i].journal, 1, Len(actions[i].journal)) = actions[i].journal
HistoryPreserved == [][AppendOnly]_vars
====
