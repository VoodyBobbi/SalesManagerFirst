"""Opt-in verified HTTPS diagnostics; never print or persist credentials.

Without a saved key this only probes public endpoints. --completion explicitly
permits one short semantic completion using the current user's stored access.
The production database is opened read-only only to read access parameters.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from sales_manager.ai.service import AIService, BASE, OAUTH
from sales_manager.storage import Store, Secrets


def probe(item):
    label, method, url, bundled = item
    try:
        with httpx.Client(verify=AIService._tls(bundled_ca=bundled),
                          timeout=httpx.Timeout(12, connect=8), follow_redirects=False) as client:
            response = client.request(method, url)
        return {'endpoint': label, 'bundled_ca': bundled, 'https_verified': True,
                'http_status': response.status_code}
    except httpx.RequestError as error:
        cause, certificate_error = error, False
        seen = set()
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            import ssl
            certificate_error |= isinstance(cause, ssl.SSLCertVerificationError)
            cause = cause.__cause__ or cause.__context__
        return {'endpoint': label, 'bundled_ca': bundled, 'https_verified': False,
                'error_type': type(error).__name__, 'certificate_error': certificate_error}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--completion', action='store_true')
    parser.add_argument('--output', default='build/gigachat-live.json')
    arguments = parser.parse_args()
    secrets = Secrets()
    names = ('gigachat_key', 'tavily_key', 'exa_key', 'serper_key', 'firecrawl_key', 'jina_key', 'mail_password')
    report = {'credential_present': {name: bool(secrets.get(name)) for name in names}}
    probes = [(label, method, url, bundled) for bundled in (False, True)
              for label, method, url in (('oauth', 'POST', OAUTH), ('models', 'GET', BASE + '/models'))]
    with ThreadPoolExecutor(max_workers=4) as pool:
        report['tls'] = list(pool.map(probe, probes))
    if report['credential_present']['gigachat_key']:
        output = Path(arguments.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        store = Store(output.parent / ('gigachat-diagnostic-' + str(time.time_ns()) + '.sqlite3'))
        production = Path(os.environ['LOCALAPPDATA']) / 'Argon' / 'SalesManagerFirst' / 'sales.sqlite3'
        if production.exists():
            with sqlite3.connect(production.resolve().as_uri() + '?mode=ro', uri=True) as connection:
                for key in ('ai.scope', 'ai.model'):
                    row = connection.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
                    if row:
                        store.set_setting(key, json.loads(row[0]))
        ai = AIService(store, secrets)
        try:
            report['authorization_and_models'] = ai.test()
            if arguments.completion:
                schema = {'type': 'object', 'properties': {'status': {'type': 'string'}},
                          'required': ['status'], 'additionalProperties': False}
                response = ai._completion(store.get_setting('ai.model', 'GigaChat-2'),
                                          'Верни JSON по схеме.', 'Проверка соединения: status OK.', schema, 'diagnostic')
                report['completion'] = bool(response.get('choices'))
        except (ValueError, RuntimeError) as error:
            # Service exceptions contain fixed safe messages, never HTTP bodies.
            report['authorization_or_completion_error'] = str(error)
        finally:
            if ai._client is not None:
                ai._client.close()
            store.close()
    else:
        report['authorization_and_models'] = 'not_run_no_key'
        report['completion'] = 'not_run_no_key'
    destination = Path(arguments.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
