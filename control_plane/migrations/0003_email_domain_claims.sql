ALTER TABLE email_identities ADD COLUMN domain_lookup_hmac TEXT;

CREATE UNIQUE INDEX email_identities_live_owned_domain
    ON email_identities (domain_lookup_hmac)
    WHERE option = 'own_domain'
      AND domain_lookup_hmac IS NOT NULL
      AND status != 'deleted';

CREATE TRIGGER email_identities_owned_domain_hmac_required
BEFORE INSERT ON email_identities
WHEN NEW.option = 'own_domain'
 AND NEW.status != 'deleted'
 AND NEW.domain_lookup_hmac IS NULL
BEGIN
    SELECT RAISE(ABORT, 'owned-domain identity requires domain lookup HMAC');
END;
