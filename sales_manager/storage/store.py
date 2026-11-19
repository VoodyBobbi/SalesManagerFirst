"""Thread-local SQLite connections and durable product workspaces."""
import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

def now():
    return datetime.now(timezone.utc).isoformat()

from .migrations import migrate

class Store:
    def __init__(self, path=None):
        if path is None:
            root = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'Argon' / 'SalesManagerFirst'
            root.mkdir(parents=True, exist_ok=True)
            path = root / 'sales.sqlite3'
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        migrate(self.db)
        self.execute("UPDATE job_runs SET status='interrupted',reason='interrupted',message='Поиск прерван закрытием приложения. Прогресс сохранён; исследование восстановится автоматически.' WHERE status='running'")
        self.execute("UPDATE job_queue SET status='pending' WHERE status='running'")
        self.execute("UPDATE outbound_attempts SET status='uncertain' WHERE status='sending'")
        self._revalidate_legacy_ai_fit()
        self._revalidate_legacy_relationships()

    def _revalidate_legacy_relationships(self):
        """Retire recognisable false automatic FITs without changing manual decisions."""
        marker = 'fit.relationship_reviewed_v5'
        if self.get_setting(marker, False):
            return
        from sales_manager.research.fit_rules import RULE_VERSION, evaluate_fit
        with self.db as db:
            rows = db.execute("SELECT f.*,p.name AS product_name FROM company_fit f JOIN products p ON p.id=f.product_id WHERE f.status='FIT' AND f.manual=0").fetchall()
            for row in rows:
                if row['ai_checked_key'].startswith('semantic-v6:'):
                    continue
                reason = row['reason'] or ''
                ai_quote = re.search(r' Основание: «(.+?)»\s*$', reason, re.S) if row['ai_checked_key'] else None
                try:
                    profile = json.loads(row['activity_profile'] or '{}')
                    if not isinstance(profile, dict):
                        continue
                    rule = profile.get('rule')
                    old_rule = profile.get('rule_version') != RULE_VERSION
                    activities = profile.get('activities')
                    activities = activities if isinstance(activities, list) else []
                    service_centre = any(isinstance(fact, dict) and re.search(r'сервисн\w*\s+центр', str(fact.get('quote', '')), re.I)
                                         for fact in activities)
                    if not ai_quote and not (old_rule and (rule == 'documented_assortment' or
                                                           (rule == 'activity:electronics' and service_centre))):
                        continue
                    ids = json.loads(row['source_ids'] or '[]')
                    if not isinstance(ids, list):
                        continue
                    pages = [dict(source) for sid in ids if type(sid) is int
                             if (source := db.execute('SELECT id AS source_id,url,text FROM sources WHERE id=?', (sid,)).fetchone())]
                    if ai_quote:
                        quote = ai_quote[1]
                        pages = [dict(page, text=quote) for page in pages if quote in page['text']]
                    if not pages:
                        continue
                    metadata = {}
                    if not ai_quote:
                        # Old profiles could record the website's generic centre
                        # quote instead of the stronger structured OSM activity.
                        for page in pages:
                            if not re.fullmatch(r'https://www\.openstreetmap\.org/(?:node|way|relation)/\d+', page['url']):
                                continue
                            tag = re.search(r'(?:^|;\s*)shop:\s*(electronics|computer|mobile_phone)(?=;|$)', page['text'])
                            if tag:
                                metadata = {'provider': 'overpass', 'source_url': page['url'],
                                            'tags': {'shop': tag[1]}, 'structured_source_id': page['source_id'],
                                            'structured_text': page['text']}
                                break
                    confirmed = evaluate_fit(row['product_name'], pages, metadata, '')['status'] == 'FIT'
                except (TypeError, ValueError):
                    continue
                if confirmed:
                    continue
                db.execute("UPDATE company_fit SET status='INSUFFICIENT_EVIDENCE',potential=1,a=NULL,reason=?,fit_key='',ai_checked_key='' WHERE product_id=? AND company_id=?",
                           ('Нужна проверка: прежняя автоматическая оценка деятельности не подтверждена. ' + reason,
                            row['product_id'], row['company_id']))
                db.execute('INSERT INTO audit_events(event,details,created_at) VALUES(?,?,?)',
                           ('legacy_relationship_review', json.dumps({'product_id': row['product_id'], 'company_id': row['company_id'],
                                                                    'previous_reason': reason}, ensure_ascii=False), now()))
            db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (marker, 'true'))

    def _revalidate_legacy_ai_fit(self):
        """Recheck only recognisable old automatic AI decisions; retain original in audit."""
        marker = 'fit.legacy_ai_reviewed_v4'
        if self.get_setting(marker, False):
            return
        from sales_manager.research.fit_rules import evaluate_fit
        with self.db as db:
            rows = db.execute("SELECT f.*,p.name AS product_name FROM company_fit f JOIN products p ON p.id=f.product_id WHERE f.status='FIT' AND f.manual=0 AND COALESCE(f.ai_checked_key,'')!='' AND f.reason LIKE '% Основание: «%' ").fetchall()
            for row in rows:
                if row['ai_checked_key'].startswith('semantic-v6:'):
                    continue
                reason = row['reason'] or ''
                match = re.search(r' Основание: «(.+?)»\s*$', reason, re.S)
                if not match:
                    continue
                quote = match.group(1)
                try:
                    ids = json.loads(row['source_ids'] or '[]')
                    if not isinstance(ids, list):
                        ids = []
                    sources = [db.execute('SELECT text FROM sources WHERE id=?', (sid,)).fetchone() for sid in ids if type(sid) is int]
                    valid_quote = any(source and quote in source['text'] for source in sources)
                    confirmed = valid_quote and evaluate_fit(row['product_name'], [{'text': quote}], {}, '')['status'] == 'FIT'
                except (TypeError, ValueError):
                    confirmed = False
                if not confirmed:
                    db.execute("UPDATE company_fit SET status='INSUFFICIENT_EVIDENCE',potential=1,a=NULL,reason=?,fit_key='',ai_checked_key='' WHERE product_id=? AND company_id=?",
                               ('Нужна проверка: прежняя оценка не подтверждает связь товара с деятельностью. Основание: «' + quote + '»', row['product_id'], row['company_id']))
                    db.execute('INSERT INTO audit_events(event,details,created_at) VALUES(?,?,?)',
                               ('legacy_ai_fit_review', json.dumps({'product_id': row['product_id'], 'company_id': row['company_id'], 'previous_reason': reason}, ensure_ascii=False), now()))
            db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (marker, 'true'))

    @property
    def db(self):
        if not hasattr(self._local, 'connection'):
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA busy_timeout=30000')
            self._local.connection = conn
        return self._local.connection

    def execute(self, sql, params=()):
        with self.db:
            cur = self.db.execute(sql, params)
        return cur.lastrowid

    def query(self, sql, params=()):
        return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        row = self.db.execute(sql, params).fetchone()
        return dict(row) if row else None

    def get_setting(self, key, default=None):
        row = self.one('SELECT value FROM settings WHERE key=?', (key,))
        if row is None:
            return default
        try:
            return json.loads(row['value'])
        except (ValueError, TypeError, UnicodeDecodeError):
            # Retain the damaged value for recovery. A savepoint also works when
            # the caller is already inside a transaction, without committing it.
            details = json.dumps({'key': key, 'value': row['value'] if isinstance(row['value'], str)
                                  else repr(row['value'])}, ensure_ascii=False)
            db = self.db
            db.execute('SAVEPOINT invalid_setting')
            try:
                db.execute('INSERT INTO audit_events(event,details,created_at) '
                           'SELECT ?,?,? WHERE NOT EXISTS '
                           '(SELECT 1 FROM audit_events WHERE event=? AND details=?)',
                           ('settings.invalid_json', details, now(), 'settings.invalid_json', details))
                db.execute('RELEASE SAVEPOINT invalid_setting')
            except Exception:
                db.execute('ROLLBACK TO SAVEPOINT invalid_setting')
                db.execute('RELEASE SAVEPOINT invalid_setting')
                raise
            return default

    def set_setting(self, key, value):
        self.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(value, ensure_ascii=False)))

    def set_company_fit_manual(self, product_id, company_id, status, reason):
        """Save a user's fit decision independently of permission to send mail."""
        if status not in ('FIT', 'NOT_FIT', 'INSUFFICIENT_EVIDENCE'):
            raise ValueError('Выберите применимость товара.')
        reason = reason.strip()
        if not reason or len(reason) > 600:
            raise ValueError('Укажите причину решения до 600 символов.')
        with self.db as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM company_fit WHERE product_id=? AND company_id=?',
                                  (product_id, company_id)).fetchone()
            if previous is None:
                raise ValueError('Компания не найдена в выбранном товаре.')
            marker = 'manual:' + hashlib.sha256(json.dumps(
                [product_id, company_id, status, reason], ensure_ascii=False).encode()).hexdigest()
            db.execute('UPDATE company_fit SET status=?,reason=?,manual=1,a=?,potential=?,checked_at=?, '
                       "fit_key=?,ai_checked_key='' WHERE product_id=? AND company_id=?",
                       (status, reason, 1 if status == 'FIT' else None,
                        1 if status == 'INSUFFICIENT_EVIDENCE' else 0, now(), marker, product_id, company_id))
            db.execute('UPDATE offers SET approved=0 WHERE product_id=? AND company_id=?', (product_id, company_id))
            details = {'product_id': product_id, 'company_id': company_id,
                       'previous_status': previous['status'], 'previous_reason': previous['reason'],
                       'status': status, 'reason': reason}
            db.execute('INSERT INTO audit_events(event,details,created_at) VALUES(?,?,?)',
                       ('fit.manual_decision', json.dumps(details, ensure_ascii=False), now()))

    def put_source(self, url, title='', text='', published_at=''):
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
        self.execute('INSERT OR IGNORE INTO sources(url,title,text,hash,fetched_at,published_at) VALUES(?,?,?,?,?,?)', (url, title, text, digest, now(), published_at))
        return self.one('SELECT id FROM sources WHERE url=? AND hash=?', (url, digest))['id']

    def ensure_product(self, name, normalized=None):
        name = ' '.join(name.split()).strip()
        if not name or len(name) > 160:
            raise ValueError('Введите название товара до 160 символов.')
        normalized = normalized or name.casefold().replace('ё', 'е')
        self.execute('INSERT OR IGNORE INTO products(name,normalized,created_at) VALUES(?,?,?)', (name, normalized, now()))
        return self.one('SELECT * FROM products WHERE normalized=?', (normalized,))

    def audit(self, event, details=''):
        self.execute('INSERT INTO audit_events(event,details,created_at) VALUES(?,?,?)', (event, details[:300], now()))

    def close(self):
        if hasattr(self._local, 'connection'):
            self._local.connection.close()
            del self._local.connection
