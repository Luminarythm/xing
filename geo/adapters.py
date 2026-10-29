"""Replaceable, side-effect-free adapters. No database or HTTP dependencies."""
from typing import Protocol
import re
from .catalog import CATALOG, VERSION
from .contracts import text, timestamp, url


class Adapter(Protocol):
    def normalize(self, record: dict) -> dict: ...


class JsonLogAdapter:
    def normalize(self, r):
        status = r.get('status')
        if type(status) is not int or not 100 <= status <= 599:
            raise ValueError('status must be an integer between 100 and 599')
        ua = r.get('user_agent', '')
        if not isinstance(ua, str) or len(ua) > 1000:
            raise ValueError('user_agent must be a string <=1000 characters')
        return dict(event_id=text(r.get('event_id'), 'event_id', 200),
                    observed_at=timestamp(r.get('observed_at')), url=url(r.get('url')),
                    status=status, user_agent=ua.strip())


class NginxJsonAdapter:
    """Input is structured Nginx JSON; the collector supplies stable event IDs."""
    def normalize(self, r):
        return JsonLogAdapter().normalize(dict(event_id=r.get('request_id'),
            observed_at=r.get('time_iso8601'), url=r.get('url'),
            status=r.get('status'), user_agent=r.get('http_user_agent', '')))


class ManualAnswerAdapter:
    def normalize(self, r):
        citations = r.get('citations', [])
        if not isinstance(citations, list) or len(citations) > 100:
            raise ValueError('citations must be a list of up to 100 URLs')
        mode = r.get('surface', 'manual')
        if mode not in ('manual', 'consumer_ui', 'model_api'):
            raise ValueError('invalid surface')
        return dict(event_id=text(r.get('event_id'), 'event_id', 200),
            observed_at=timestamp(r.get('observed_at')),
            platform=text(r.get('platform'), 'platform', 100),
            model=text(r.get('model', 'unknown'), 'model', 100), surface=mode,
            prompt=text(r.get('prompt'), 'prompt', 5000),
            answer=text(r.get('answer'), 'answer', 50000),
            citations=sorted(set(url(u) for u in citations)),
            evidence_url=url(r['evidence_url']) if r.get('evidence_url') else None,
            locale=text(r.get('locale', 'zh-CN'), 'locale', 50))


LOG_ADAPTERS = {'json-v1': JsonLogAdapter(), 'nginx-json-v1': NginxJsonAdapter()}
ANSWER_ADAPTERS = {'manual-v1': ManualAnswerAdapter()}


def classify(ua):
    matches = [entry for entry in CATALOG if entry['enabled'] and
               re.search(r'(?<![\w-])' + re.escape(entry['token']) + r'(?![\w-])', ua, re.I)]
    entry = matches[0] if len(matches) == 1 else None
    return dict(declared_bot=entry['token'] if entry else None,
                verification='unverified' if entry else 'unknown', rule_version=VERSION,
                crawler_category=entry['category'] if entry else 'unknown',
                crawler_provider=entry['provider'] if entry else None,
                crawler_evidence=entry['evidence_level'] if entry else None,
                crawler_source=entry['source_url'] if entry else None,
                ambiguous_tokens=[e['token'] for e in matches] if len(matches) > 1 else [])
