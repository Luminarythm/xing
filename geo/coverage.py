"""Pure URL-set coverage calculations over an explicit inventory snapshot."""


def coverage(inventory, ai_logs):
    if inventory is None:
        return dict(inventory_id=None, inventory_size=None, observed_coverage_rate=None,
                    successful_coverage_rate=None, pages=[], outside_inventory_urls=None)
    urls = set(inventory['urls'])
    grouped = {}
    for event in ai_logs:
        grouped.setdefault(event['url'], []).append(event)
    pages = []
    for address in sorted(urls):
        events = grouped.get(address, [])
        success = sum(200 <= e['status'] < 300 for e in events)
        failed = sum(e['status'] >= 400 for e in events)
        redirects = sum(300 <= e['status'] < 400 for e in events)
        pages.append(dict(url=address, requests=len(events), successful_requests=success,
                          failed_requests=failed, redirect_requests=redirects,
                          observed=bool(events), successful=bool(success)))
    observed = sum(p['observed'] for p in pages)
    successful = sum(p['successful'] for p in pages)
    return dict(inventory_id=inventory['id'], inventory_size=len(urls),
                inventory_created_at=inventory['created_at'], inventory_source=inventory['source'],
                observed_pages=observed, successful_pages=successful,
                unobserved_pages=len(urls)-observed,
                observed_coverage_rate=observed/len(urls) if urls else None,
                successful_coverage_rate=successful/len(urls) if urls else None,
                pages=pages, outside_inventory_urls=sorted(set(grouped)-urls))
