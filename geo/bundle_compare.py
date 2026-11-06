"""Compare two validated local report bundles without treating unknown values as zero."""
import argparse
from datetime import datetime
import json

from .bundle_check import verify, require
from .limits import Limits, CapacityExceeded, json_bytes


def difference(before, after, reason=None, rate=False):
    if reason is None and (before is None or after is None):
        reason = 'unknown_value'
    return dict(before=before, after=after, delta=None if reason else
                (after - before) * (100 if rate else 1),
                unit='percentage_points' if rate else 'count', comparable=reason is None, reason=reason)


def compare(before_path, after_path, max_bytes=16 * 1024 * 1024):
    before, after = verify(before_path, max_bytes), verify(after_path, max_bytes)
    bm, am = before['manifest'], after['manifest']
    br, ar = before['report'], after['report']
    bq, aq = before['quality'], after['quality']
    for report in (br, ar):
        require(isinstance(report['site'].get('origin'), str) and bool(report['site']['origin'])
                and isinstance(report.get('mention_rule'), str) and bool(report['mention_rule']))
    require(bm['site_id'] == am['site_id']
            and br['site']['origin'] == ar['site']['origin'])
    require(bm['filters'] == am['filters'] and bm['quality_filters'] == am['quality_filters'])
    bw, aw = bm['window'], am['window']
    # Require chronological disjoint windows. Comparing two revisions of one window is a different operation.
    require(datetime.fromisoformat(bw['end']) <= datetime.fromisoformat(aw['start']))
    duration = lambda w: (datetime.fromisoformat(w['end']) - datetime.fromisoformat(w['start'])).total_seconds()
    brand_changed = (bm['brand_config_id'] != am['brand_config_id']
                     or br['brand_definitions'] != ar['brand_definitions']
                     or br['mention_rule'] != ar['mention_rule'])
    inventory_changed = (bm['inventory_id'] != am['inventory_id']
                         or br['coverage']['inventory_size'] != ar['coverage']['inventory_size']
                         or {p['url'] for p in br['coverage']['pages']} != {p['url'] for p in ar['coverage']['pages']})
    brand_reason = 'brand_configuration_changed' if brand_changed else None
    coverage_reason = 'inventory_changed' if inventory_changed else None
    warnings = []
    if duration(bw) != duration(aw):
        warnings.append('unequal_window_duration')
    if bw['end'] != aw['start']:
        warnings.append('gap_between_windows')
    if bq['samples'] != aq['samples']:
        warnings.append('sample_denominator_changed')
    if brand_changed:
        warnings.append(brand_reason)
    if inventory_changed:
        warnings.append(coverage_reason)
    metrics = {key: difference(br[key], ar[key]) for key in
               ('answer_samples', 'cited_site_samples', 'log_requests', 'declared_ai_requests', 'failed_ai_requests')}
    metrics['citation_rate'] = difference(br['citation_rate'], ar['citation_rate'], rate=True)
    metrics['mentioned_samples'] = difference(br['mentioned_samples'], ar['mentioned_samples'], brand_reason)
    for key in ('coverage_rate', 'successful_coverage_rate'):
        metrics[key] = difference(br[key], ar[key], coverage_reason, rate=True)
    quality = {}
    for key in ('without_citations', 'without_evidence_url', 'unknown_model'):
        b, a = bq[key], aq[key]
        quality[key] = dict(before=b, after=a, rate_change=difference(b['rate'], a['rate'], rate=True),
                            denominator_changed=b['evaluated_samples'] != a['evaluated_samples'])
    brands = {name: difference(br['brand_mentions'].get(name), ar['brand_mentions'].get(name), brand_reason)
              for name in sorted(set(br['brand_mentions']) | set(ar['brand_mentions']))}
    result = dict(format='xing-report-comparison', version=1, site_id=bm['site_id'], filters=bm['filters'],
        before=dict(window=bw, generated_at=bm['generated_at'], brand_config_id=bm['brand_config_id'],
                    inventory_id=bm['inventory_id'], samples=bq['samples'], synthetic_samples=bq['synthetic_samples'],
                    unclassified_samples=bq['unclassified_samples'], duration_seconds=duration(bw)),
        after=dict(window=aw, generated_at=am['generated_at'], brand_config_id=am['brand_config_id'],
                   inventory_id=am['inventory_id'], samples=aq['samples'], synthetic_samples=aq['synthetic_samples'],
                   unclassified_samples=aq['unclassified_samples'], duration_seconds=duration(aw)),
        warnings=warnings, metrics=metrics, quality=quality, brands=brands,
        limitations=['Deltas describe retained samples, not causal effects or market-wide exposure.',
                     'Unequal durations are not normalized; counts are descriptive totals.',
                     'Review decisions and retained records can differ between independently exported snapshots.',
                     'A matching configuration ID does not prove data authenticity; unclassified is not verified real.'])
    json_bytes(result, max_bytes)
    return result


def summary(result):
    lines = ['双时间窗报告对照（描述性变化）']
    for side, title in (('before', '前期'), ('after', '后期')):
        item = result[side]
        lines.append(f"{title}：{item['window']['start']} 至 {item['window']['end']}（不含结束）；"
                     f"样本 {item['samples']}，显式合成 {item['synthetic_samples']}，未分类 {item['unclassified_samples']}")
    lines.append('口径提示：' + json.dumps(result['warnings'], ensure_ascii=True))
    for key, title in (('answer_samples', '回答样本'), ('citation_rate', '站点引用率'),
                       ('mentioned_samples', '品牌提及样本'), ('coverage_rate', '观察覆盖率')):
        metric = result['metrics'][key]
        change = ('不计算（' + metric['reason'] + '）') if metric['delta'] is None else (
            f"{metric['delta']:+.2f} 个百分点" if metric['unit'] == 'percentage_points'
            else f"{metric['delta']:+d}")
        lines.append(title + '：' + change)
    lines.append('未知值不作零；配置或清单变化时暂停相应差值。变化不证明优化效果或真实曝光。')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('before')
    parser.add_argument('after')
    parser.add_argument('--summary', action='store_true')
    args = parser.parse_args()
    try:
        result = compare(args.before, args.after, Limits.from_env().response_bytes)
    except (OSError, ValueError, CapacityExceeded):
        parser.exit(1, 'Bundle comparison failed: validate bundles, site, filters and chronological non-overlapping windows.\n')
    print(summary(result) if args.summary else json.dumps(result, ensure_ascii=True, allow_nan=False))


if __name__ == '__main__':
    main()
