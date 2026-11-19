"""Provider-reported quotas only; never infer balance from local request counts.

Official references:
https://docs.tavily.com/documentation/api-reference/endpoint/usage
https://docs.firecrawl.dev/api-reference/endpoint/credit-usage
"""
import math


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def fetch_quota(name, key, request):
    """Return remaining credits from the account endpoint, or unknown (None)."""
    if not key or name not in ('tavily', 'firecrawl'):
        return None
    headers = {'Authorization': 'Bearer ' + key}
    if name == 'firecrawl':
        response = request('GET', 'https://api.firecrawl.dev/v2/team/credit-usage', headers=headers)
        data = response.get('data')
        if response.get('success') is not True or not isinstance(data, dict):
            return None
        return _number(data.get('remainingCredits'))
    response = request('GET', 'https://api.tavily.com/usage', headers=headers)
    account, api_key = response.get('account'), response.get('key')
    if not isinstance(account, dict) or not isinstance(api_key, dict):
        return None
    plan_limit, plan_usage = _number(account.get('plan_limit')), _number(account.get('plan_usage'))
    key_limit, key_usage = _number(api_key.get('limit')), _number(api_key.get('usage'))
    unlimited_key = 'limit' in api_key and api_key['limit'] is None
    if None in (plan_limit, plan_usage, key_usage) or (key_limit is None and not unlimited_key):
        return None
    # Pay-as-you-go capacity is deliberately excluded from the available plan quota.
    remaining = plan_limit - plan_usage
    return max(0, remaining if unlimited_key else min(remaining, key_limit - key_usage))
