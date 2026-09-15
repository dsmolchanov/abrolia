-- A Gmail household receives mail through forwarding the family turns on in
-- Gmail, into a hidden Nerve relay inbox. This is the runtime's record of that
-- forwarding: whether the relay has ever delivered a letter or a check message,
-- and the confirmation Google sent that a human has to open. No credential and
-- no letter content lives here; the confirmation link is a one-time Google URL
-- whose only effect is to approve forwarding to this household's own relay.
CREATE TABLE IF NOT EXISTS gmail_forwarding_state (
    binding_identity_id   TEXT NOT NULL,
    binding_revision      INTEGER NOT NULL,
    state                 TEXT NOT NULL, -- pending|active|stale
    confirmation_link     TEXT,
    confirmation_at       REAL,
    confirmation_shown_at REAL,
    last_letter_at        REAL,
    last_check_at         REAL,
    created_at            REAL NOT NULL,
    updated_at            REAL NOT NULL,
    PRIMARY KEY (binding_identity_id, binding_revision)
);
