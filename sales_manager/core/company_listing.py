"""The shared unfiltered company list used by research results and the UI."""
from .contacts import company_emails


def visible_companies(store, product_id, include_rejected=False, include_history=False):
    rows = store.query('''SELECT c.*,f.status AS fit_status,f.reason,f.manual AS fit_manual,f.potential,
      (SELECT value FROM company_contacts cc WHERE cc.company_id=c.id AND kind='email' ORDER BY id LIMIT 1) AS email,
      (SELECT value FROM company_contacts cc WHERE cc.company_id=c.id AND kind='phone' ORDER BY id LIMIT 1) AS phone,
      (SELECT id FROM offers o WHERE o.product_id=f.product_id AND o.company_id=c.id) AS offer_id,
      EXISTS(SELECT 1 FROM offers o JOIN outbound_attempts a ON a.offer_id=o.id WHERE o.product_id=f.product_id AND o.company_id=c.id AND a.status='sent') AS sent,
      EXISTS(SELECT 1 FROM offers o JOIN outbound_attempts a ON a.offer_id=o.id WHERE o.product_id=f.product_id AND o.company_id=c.id AND a.status IN ('sending','uncertain')) AS uncertain,
      EXISTS(SELECT 1 FROM mail_messages m WHERE m.product_id=f.product_id AND m.company_id=c.id AND m.is_reply=1) AS replied,
      (? AND EXISTS(SELECT 1 FROM mail_messages m WHERE m.product_id=f.product_id AND m.company_id=c.id)) AS has_history
      FROM companies c JOIN company_fit f ON f.company_id=c.id WHERE f.product_id=? AND
      (f.manual=1 OR has_history OR (? AND f.status='NOT_FIT') OR
       f.status='INSUFFICIENT_EVIDENCE' OR (f.status='FIT' AND
        EXISTS(SELECT 1 FROM company_contacts cc WHERE cc.company_id=c.id AND cc.kind IN ('email','phone') AND cc.value<>'')))
      ORDER BY CASE WHEN f.status='FIT' THEN 0 ELSE 1 END,c.name''',
                       (int(include_history), product_id, int(include_rejected)))
    for row in rows:
        contacts = company_emails(store, row['id'])
        row['email'] = contacts[0]['email'] if contacts else ''
    return [row for row in rows if row['fit_status'] == 'INSUFFICIENT_EVIDENCE'
            or row['fit_manual'] or row['has_history'] or row['email'] or row['phone']
            or (include_rejected and row['fit_status'] == 'NOT_FIT')]
