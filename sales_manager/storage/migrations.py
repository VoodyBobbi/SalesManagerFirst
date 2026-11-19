"""Versioned, atomic SQLite schema migrations."""
SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY,name TEXT NOT NULL,normalized TEXT UNIQUE NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS product_variants(id INTEGER PRIMARY KEY,product_id INTEGER REFERENCES products(id),name TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS sources(id INTEGER PRIMARY KEY,url TEXT NOT NULL,title TEXT DEFAULT '',text TEXT DEFAULT '',hash TEXT NOT NULL,fetched_at TEXT NOT NULL,published_at TEXT DEFAULT '',UNIQUE(url,hash));
CREATE TABLE IF NOT EXISTS market_signals(id INTEGER PRIMARY KEY,product_id INTEGER REFERENCES products(id),source_id INTEGER REFERENCES sources(id),country TEXT,kind TEXT,description TEXT,score REAL DEFAULT 0,created_at TEXT,UNIQUE(product_id,source_id,country,kind));
CREATE TABLE IF NOT EXISTS companies(id INTEGER PRIMARY KEY,name TEXT NOT NULL,domain TEXT DEFAULT '',url TEXT DEFAULT '',country TEXT DEFAULT '',legal_id TEXT DEFAULT '',manual INTEGER DEFAULT 0,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS companies_domain ON companies(domain);
CREATE TABLE IF NOT EXISTS company_contacts(id INTEGER PRIMARY KEY,company_id INTEGER REFERENCES companies(id),kind TEXT NOT NULL,value TEXT NOT NULL,source_id INTEGER REFERENCES sources(id),checked_at TEXT,UNIQUE(company_id,kind,value));
CREATE TABLE IF NOT EXISTS company_fit(product_id INTEGER REFERENCES products(id),company_id INTEGER REFERENCES companies(id),status TEXT NOT NULL,reason TEXT DEFAULT '',source_ids TEXT DEFAULT '[]',manual INTEGER DEFAULT 0,a REAL,b REAL,c REAL,checked_at TEXT,PRIMARY KEY(product_id,company_id));
CREATE TABLE IF NOT EXISTS permissions(id INTEGER PRIMARY KEY,company_id INTEGER REFERENCES companies(id),email TEXT NOT NULL,basis TEXT NOT NULL,evidence TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS opt_out(email TEXT PRIMARY KEY,reason TEXT DEFAULT '',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS offers(id INTEGER PRIMARY KEY,product_id INTEGER REFERENCES products(id),company_id INTEGER REFERENCES companies(id),subject TEXT NOT NULL,body TEXT NOT NULL,reason TEXT DEFAULT '',source_ids TEXT DEFAULT '[]',cache_key TEXT DEFAULT '',approved INTEGER DEFAULT 0,edited INTEGER DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(product_id,company_id));
CREATE TABLE IF NOT EXISTS mail_accounts(id INTEGER PRIMARY KEY CHECK(id=1),email TEXT NOT NULL,provider TEXT,imap_host TEXT,imap_port INTEGER DEFAULT 993,smtp_host TEXT,smtp_port INTEGER DEFAULT 465,connected INTEGER DEFAULT 0,uidvalidity TEXT DEFAULT '',last_uid INTEGER DEFAULT 0,sent_folder TEXT DEFAULT '',updated_at TEXT);
CREATE TABLE IF NOT EXISTS outbound_attempts(id INTEGER PRIMARY KEY,offer_id INTEGER REFERENCES offers(id),idempotency_key TEXT UNIQUE NOT NULL,message_id TEXT UNIQUE NOT NULL,recipient TEXT NOT NULL,status TEXT NOT NULL,error TEXT DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mail_messages(id INTEGER PRIMARY KEY,product_id INTEGER REFERENCES products(id),company_id INTEGER REFERENCES companies(id),message_id TEXT UNIQUE NOT NULL,in_reply_to TEXT DEFAULT '',refs TEXT DEFAULT '',direction TEXT NOT NULL,sender TEXT,recipient TEXT,subject TEXT,body TEXT,sent_at TEXT,is_reply INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS mail_branch ON mail_messages(product_id,company_id,sent_at);
CREATE TABLE IF NOT EXISTS usage_log(id INTEGER PRIMARY KEY,provider TEXT NOT NULL,operation TEXT NOT NULL,input_tokens INTEGER DEFAULT 0,output_tokens INTEGER DEFAULT 0,credits REAL DEFAULT 0,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS job_runs(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,product_id INTEGER REFERENCES products(id),status TEXT NOT NULL,countries TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS job_queue(id INTEGER PRIMARY KEY,run_id INTEGER REFERENCES job_runs(id),task_key TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',error TEXT DEFAULT '',UNIQUE(run_id,task_key));
CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY,value TEXT NOT NULL,expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit_events(id INTEGER PRIMARY KEY,event TEXT NOT NULL,details TEXT DEFAULT '',created_at TEXT NOT NULL);
'''

MARKET_AND_FIT = '''
ALTER TABLE company_fit ADD COLUMN potential INTEGER NOT NULL DEFAULT 0;
ALTER TABLE company_fit ADD COLUMN activity_profile TEXT NOT NULL DEFAULT '{}';
ALTER TABLE company_fit ADD COLUMN fit_key TEXT NOT NULL DEFAULT '';
ALTER TABLE company_fit ADD COLUMN ai_checked_key TEXT NOT NULL DEFAULT '';
CREATE TABLE market_observations(
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    source_id INTEGER NOT NULL REFERENCES sources(id),
    metric TEXT NOT NULL,
    value REAL,
    unit TEXT NOT NULL DEFAULT '',
    period TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT '',
    excerpt TEXT NOT NULL,
    measured INTEGER NOT NULL DEFAULT 0 CHECK(measured IN (0,1)),
    event_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(product_id,event_key)
);
CREATE INDEX market_observations_market ON market_observations(country,metric,unit,period);
'''

MARKET_SOURCES = '''
CREATE TABLE market_observation_sources(
    observation_id INTEGER NOT NULL REFERENCES market_observations(id),
    source_id INTEGER NOT NULL REFERENCES sources(id),
    excerpt TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(observation_id,source_id)
);
INSERT INTO market_observation_sources(observation_id,source_id,excerpt,published_at)
SELECT id,source_id,excerpt,published_at FROM market_observations;
CREATE INDEX market_observation_sources_source ON market_observation_sources(source_id);
'''

PREDICTABLE_ACTIONS = '''
ALTER TABLE offers ADD COLUMN recipient TEXT NOT NULL DEFAULT '';
ALTER TABLE job_runs ADD COLUMN reason TEXT NOT NULL DEFAULT '';
ALTER TABLE job_runs ADD COLUMN message TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX outbound_single_active ON outbound_attempts(offer_id)
WHERE status IN ('sending','uncertain');
INSERT INTO settings(key,value) VALUES('ai.unconfirmed_tokens','0')
ON CONFLICT(key) DO UPDATE SET value='0';
'''

MARKET_PROGRESS = '''
CREATE TABLE job_source_responses(
    run_id INTEGER NOT NULL REFERENCES job_runs(id),
    provider TEXT NOT NULL,
    first_success_at TEXT NOT NULL,
    PRIMARY KEY(run_id,provider)
);
CREATE TABLE job_diagnostics(
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES job_runs(id),
    provider TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX job_diagnostics_run ON job_diagnostics(run_id,id);
'''

COMPANY_LOCATIONS = '''
ALTER TABLE companies ADD COLUMN branch_name TEXT NOT NULL DEFAULT '';
ALTER TABLE companies ADD COLUMN address TEXT NOT NULL DEFAULT '';
ALTER TABLE companies ADD COLUMN region TEXT NOT NULL DEFAULT '';
ALTER TABLE companies ADD COLUMN organization_key TEXT NOT NULL DEFAULT '';
ALTER TABLE companies ADD COLUMN identity_key TEXT NOT NULL DEFAULT '';
ALTER TABLE company_contacts ADD COLUMN scope TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX company_identity_key ON companies(identity_key) WHERE identity_key<>'';
CREATE INDEX company_organization_key ON companies(organization_key) WHERE organization_key<>'';
'''

MIGRATIONS = {1: SCHEMA, 2: MARKET_AND_FIT, 3: MARKET_SOURCES, 4: PREDICTABLE_ACTIONS,
              5: MARKET_PROGRESS, 6: COMPANY_LOCATIONS}

def migrate(connection):
    current = connection.execute('PRAGMA user_version').fetchone()[0]
    if current > max(MIGRATIONS):
        raise RuntimeError('База создана более новой версией. Обновите приложение.')
    for version, script in sorted(MIGRATIONS.items()):
        if version > current:
            try:
                connection.executescript('BEGIN IMMEDIATE;\n' + script + f'\nPRAGMA user_version={version};\nCOMMIT;')
            except Exception:
                connection.rollback()
                raise
