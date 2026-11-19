"""Prove free API access from provider-reported billing data, never a checkbox.

Only non-search account endpoints are used for verification. A credit balance
does not establish that the credits are free or that automatic billing is off.
The verification result is short-lived and tied to the exact API credential.
"""
import hashlib
import time

from .quota import _number


PROOF_VERSION = 'free-access-v1'
PROOF_TTL = 600
DOCUMENTATION = {
    'tavily': (
        'https://docs.tavily.com/documentation/api-reference/endpoint/usage',
        'https://docs.tavily.com/documentation/api-credits',
    ),
    'firecrawl': (
        'https://docs.firecrawl.dev/api-reference/endpoint/credit-usage',
        'https://docs.firecrawl.dev/billing',
    ),
    'exa': (
        'https://exa.ai/docs/reference/team-management/get-team-credits',
        'https://exa.ai/docs/admin/billing',
    ),
    'serper': ('https://serper.dev/',),
    'jina': ('https://docs.jina.ai/', 'https://jina.ai/api-dashboard/pricing/'),
}


def credential_hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def free_access_allowed(name, key, record):
    """A stale proof, replacement key or exhausted allowance never allows search."""
    if not isinstance(record, dict) or not key:
        return False
    remaining = _number(record.get('remaining'))
    expiry = _number(record.get('expires_at'))
    checked = _number(record.get('checked_at'))
    stamp = time.time()
    return bool(record.get('allowed') is True and record.get('provider') == name
                and name == 'tavily'
                and record.get('proof_version') == PROOF_VERSION
                and record.get('key_hash') == credential_hash(key)
                and checked is not None and 0 <= stamp - checked <= PROOF_TTL
                and expiry is not None and stamp < expiry <= checked + PROOF_TTL
                and remaining is not None and remaining >= 1)


def free_access_valid(record, key):
    """Convenient predicate for a saved proof before provider construction."""
    return free_access_allowed(record.get('provider') if isinstance(record, dict) else None,
                               key, record)


def verify_free_access(name, key, request):
    """Return only confirmed free allowance, without making a search request.

    ``request`` has the same JSON interface as ResearchService._json. Transport,
    authentication and quota errors intentionally propagate as ProviderError;
    the caller can display the correct error and invalidate any previous proof.
    Raw API responses and API keys are never returned for storage or diagnostics.
    """
    stamp = time.time()
    result = dict(provider=name, allowed=False, remaining=None, key_valid=None,
                  key_hash=credential_hash(key), checked_at=stamp,
                  expires_at=stamp + PROOF_TTL, proof_version=PROOF_VERSION,
                  evidence=list(DOCUMENTATION.get(name, ())), reason='')
    if not key:
        result['reason'] = 'Введите API-ключ.'
        return result
    if name == 'tavily':
        response = request('GET', 'https://api.tavily.com/usage',
                           headers={'Authorization': 'Bearer ' + key})
        account = response.get('account') if isinstance(response, dict) else None
        api_key = response.get('key') if isinstance(response, dict) else None
        if not isinstance(account, dict) or not isinstance(api_key, dict):
            result['reason'] = 'Tavily не вернул сведения о тарифе и лимитах. Поиск не включён.'
            return result
        result['key_valid'] = True
        plan = account.get('current_plan')
        if not isinstance(plan, str) or plan.strip().casefold() != 'researcher':
            result['reason'] = 'Tavily: бесплатный тариф Researcher не подтверждён. Проверьте тариф в кабинете.'
            return result
        paygo_limit = _number(account.get('paygo_limit'))
        paygo_usage = _number(account.get('paygo_usage'))
        if paygo_limit != 0 or paygo_usage != 0:
            result['reason'] = 'Tavily: платный Pay-as-you-go не подтверждён как отключённый. Отключите его в кабинете и повторите проверку.'
            return result
        plan_limit = _number(account.get('plan_limit'))
        plan_usage = _number(account.get('plan_usage'))
        key_usage = _number(api_key.get('usage'))
        key_limit = _number(api_key.get('limit'))
        # A null key.limit is explicitly documented as an unlimited key. The
        # account free allowance still caps access because PAYG is disabled.
        unlimited_key = 'limit' in api_key and api_key['limit'] is None
        if (None in (plan_limit, plan_usage, key_usage)
                or (key_limit is None and not unlimited_key)):
            result['reason'] = 'Tavily: остаток бесплатных запросов неизвестен. Поиск не включён.'
            return result
        available = plan_limit - plan_usage
        if not unlimited_key:
            available = min(available, key_limit - key_usage)
        result['remaining'] = max(0, available)
        result['allowed'] = result['remaining'] >= 1
        result['reason'] = ('Tavily: подтверждён бесплатный Researcher, Pay-as-you-go отключён.'
                            if result['allowed'] else 'Tavily: бесплатная квота исчерпана. Используются остальные источники.')
        return result
    if name == 'firecrawl':
        response = request('GET', 'https://api.firecrawl.dev/v2/team/credit-usage',
                           headers={'Authorization': 'Bearer ' + key})
        data = response.get('data') if isinstance(response, dict) else None
        if isinstance(response, dict) and response.get('success') is True and isinstance(data, dict):
            result['key_valid'] = True
            # Kept for diagnostics only. It must not be confused with a verified
            # free allowance: paid plans and PAYG use the same balance field.
            result['reported_balance'] = _number(data.get('remainingCredits'))
        result['reason'] = ('Firecrawl: API баланса не сообщает бесплатный тариф и состояние '
                            'Pay-as-you-go. Поиск с ключом не включён; доступен поиск без ключа.')
        return result
    reasons = {
        'exa': 'Exa: API остатка не подтверждает происхождение бесплатных кредитов и запрет автосписаний. Поиск с ключом не включён.',
        'serper': 'Serper: нет документированной проверки, отличающей бесплатные запросы от купленных кредитов. Поиск с ключом не включён.',
        'jina': 'Jina: опубликованный API не подтверждает бесплатный баланс и отключённое автопополнение. Поиск с ключом не включён.',
    }
    result['reason'] = reasons.get(name, 'Для этого источника нет проверки бесплатного доступа.')
    return result
