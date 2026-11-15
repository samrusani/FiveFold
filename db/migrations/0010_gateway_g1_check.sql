-- G1's check before a send (decision record 0008).
--
-- G1 is authorization only: before its one send it asks the approvals service for the
-- approval (0002, rule 17) and reads no fact. Its attempt rows say so with a check kind of
-- their own, `authority_only`, which records the approval it found and no basis.

ALTER TABLE gw.attempt DROP CONSTRAINT attempt_check_kind_check;

ALTER TABLE gw.attempt ADD CONSTRAINT attempt_check_kind_check
    CHECK (check_kind IN ('full', 'exact_only', 'exempt', 'authority_only'));

ALTER TABLE gw.attempt ADD CONSTRAINT attempt_authority_only_checks_no_fact
    CHECK (check_kind <> 'authority_only'
        OR (check_approval IS NOT NULL AND check_basis IS NULL));
