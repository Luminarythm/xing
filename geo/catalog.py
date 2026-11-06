"""Reviewed evidence registry, not an IP/DNS identity verification service."""
from copy import deepcopy

VERSION = '3'
REVIEWED_AT = '2026-10-29'
CF = 'https://developers.cloudflare.com/ai-crawl-control/reference/bots/'


def entry(token, provider, category, source_url, evidence_level='infrastructure_documentation', enabled=True):
    return dict(token=token, provider=provider, category=category, source_url=source_url,
                evidence_level=evidence_level, enabled=enabled, reviewed_at=REVIEWED_AT,
                rule_version=VERSION, verification_method='not_implemented')


CATALOG = [
    entry('GPTBot', 'OpenAI', 'ai_crawler', CF),
    entry('OAI-SearchBot', 'OpenAI', 'ai_search', CF),
    entry('ChatGPT-User', 'OpenAI', 'ai_assistant', CF),
    entry('ClaudeBot', 'Anthropic', 'ai_crawler', CF),
    entry('PerplexityBot', 'Perplexity', 'ai_search', CF),
    entry('Bytespider', 'ByteDance', 'ai_crawler', CF),
    entry('Baiduspider', 'Baidu', 'search_engine', 'https://www.baidu.com/search/robots_english.html', 'operator_documentation'),
]

# Candidate names are investigation leads only, never enabled through name inference.
PENDING = [dict(provider=p, status='pending_primary_evidence', enabled=False, reviewed_at=REVIEWED_AT)
           for p in ('Kimi', 'Doubao product-specific bots', 'DeepSeek', 'Tongyi', 'Tencent Hunyuan')]


def catalog():
    return deepcopy(dict(version=VERSION, reviewed_at=REVIEWED_AT, entries=CATALOG, pending=PENDING))


def is_ai_event(event):
    if 'crawler_category' in event:
        return event['crawler_category'] in ('ai_crawler', 'ai_search', 'ai_assistant')
    # Preserve the meaning of the small v1/v2 catalog without reclassifying history.
    return event.get('declared_bot') in ('GPTBot', 'OAI-SearchBot', 'ChatGPT-User', 'ClaudeBot', 'PerplexityBot')
