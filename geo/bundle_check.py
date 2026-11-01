"""Offline bounded bundle validation and a plain-text Chinese summary; never extracts files."""
import argparse
import hashlib
import io
import json
import math
from pathlib import Path
import stat
import zipfile
import zlib

from .contracts import timestamp
from .limits import Limits
from .reporting import normalize_filters


MEMBERS = {'manifest.json', 'report.json', 'quality.json', 'README.txt'}


def require(condition):
    if not condition:
        raise ValueError('invalid or inconsistent report bundle')


def integer(value):
    require(type(value) is int and value >= 0)
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError('non-finite JSON number')


def ratio(actual, numerator, denominator):
    if denominator == 0:
        require(actual is None)
    else:
        require(type(actual) in (int, float) and math.isfinite(actual)
                and math.isclose(actual, numerator / denominator, rel_tol=1e-12, abs_tol=1e-12))


def validate_reports(manifest, basic, quality):
    require(manifest['format'] == 'xing-report-bundle' and type(manifest['version']) is int
            and manifest['version'] == 1 and type(manifest['schema_version']) is int
            and manifest['schema_version'] == 8)
    require(manifest['consistency'] == 'single_sqlite_read_snapshot'
            and manifest['scope'] == 'current_retained_data')
    timestamp(manifest['generated_at'])
    window = manifest['window']
    require(set(window) == {'start', 'end', 'end_exclusive'} and window['end_exclusive'] is True)
    require(timestamp(window['start']) == window['start'] and timestamp(window['end']) == window['end']
            and window['start'] < window['end'])
    require(window == basic['window'] == quality['window'])
    require(isinstance(manifest['site_id'], str) and bool(manifest['site_id'])
            and manifest['site_id'] == basic['site']['id'] == quality['site_id'])
    filters = manifest['filters']
    require(filters == normalize_filters(filters) and filters.get('kind') != 'logs')
    require(filters == basic['filters'] and manifest['quality_filters'] == quality['filters']
            == dict(filters, kind='answers'))
    require(manifest['quality_dataset'] == quality['dataset'] == 'all'
            and quality['scope'] == 'current_retained_answers')
    require(manifest['brand_config_id'] == basic['brand_config_id']
            and manifest['inventory_id'] == basic['coverage']['inventory_id'])
    require(basic['answer_scope'] == 'included')
    total = integer(quality['samples'])
    require(integer(basic['answer_samples']) == total)
    require(integer(quality['synthetic_samples']) + integer(quality['unclassified_samples']) == total)
    require(set(quality['by_surface']) == {'manual', 'consumer_ui', 'model_api', 'unknown'})
    require(sum(integer(v) for v in quality['by_surface'].values()) == total)
    for name in ('without_citations', 'without_evidence_url', 'unknown_model'):
        metric = quality[name]
        count, evaluated, unknown = (integer(metric[k]) for k in ('count', 'evaluated_samples', 'unknown_samples'))
        require(count <= evaluated and evaluated + unknown == total)
        ratio(metric['rate'], count, evaluated)
    for name in ('mentioned_samples', 'cited_site_samples', 'ambiguous_mention_samples'):
        require(integer(basic[name]) <= total)
    ratio(basic['citation_rate'], basic['cited_site_samples'], total)
    for name in ('evidence_counts_by_surface', 'evidence_counts_by_platform'):
        require(sum(integer(v) for v in basic[name].values()) == total)
    require(isinstance(basic['site']['name'], str))


def verify(path, max_bytes=16 * 1024 * 1024):
    require(type(max_bytes) is int and max_bytes >= 1024)
    # Bound the archive before ZipFile parses its central directory; never trust its manifest limits.
    with Path(path).open('rb') as stream:
        packed = stream.read(max_bytes + 65537)
    require(len(packed) <= max_bytes + 65536)
    try:
        with zipfile.ZipFile(io.BytesIO(packed)) as archive:
            entries = archive.infolist()
            require(len(entries) == 4 and {entry.filename for entry in entries} == MEMBERS)
            total, files = 0, {}
            for entry in entries:
                require(entry.orig_filename == entry.filename and not entry.is_dir()
                        and not entry.flag_bits & 1
                        and entry.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                        and stat.S_IFMT(entry.external_attr >> 16) in (0, stat.S_IFREG))
                require(0 <= entry.file_size <= max_bytes - total)
                with archive.open(entry) as member:
                    data = member.read(max_bytes - total + 1)
                require(len(data) == entry.file_size and total + len(data) <= max_bytes)
                files[entry.filename] = data
                total += len(data)
        decoded = {name: json.loads(files[name].decode('utf-8'), object_pairs_hook=unique_object,
                                   parse_constant=invalid_constant)
                   for name in MEMBERS if name.endswith('.json')}
        files['README.txt'].decode('utf-8')
        manifest, basic, quality = (decoded[name] for name in ('manifest.json', 'report.json', 'quality.json'))
        require(set(manifest['files']) == MEMBERS - {'manifest.json'})
        for name, info in manifest['files'].items():
            require(integer(info['bytes']) == len(files[name])
                    and info['sha256'] == hashlib.sha256(files[name]).hexdigest())
        budgets = manifest['limits']
        require(integer(budgets['query_events']) > 0 and integer(budgets['query_bytes']) >= 1024
                and integer(budgets['uncompressed_bundle_bytes']) >= total)
        validate_reports(manifest, basic, quality)
    except (KeyError, TypeError, AttributeError, RecursionError, OverflowError,
            zipfile.BadZipFile, NotImplementedError, RuntimeError, UnicodeError, zlib.error, EOFError) as error:
        raise ValueError('invalid report bundle') from error
    return dict(manifest=manifest, report=basic, quality=quality, uncompressed_bytes=total)


def summary(checked):
    basic, quality, manifest = (checked[k] for k in ('report', 'quality', 'manifest'))
    # Escape untrusted metadata including terminal controls; no Markdown/HTML execution.
    label = lambda value: json.dumps(value, ensure_ascii=True)
    percent = lambda value: '未知（无可计算分母）' if value is None else f'{value:.1%}'
    lines = ['本地 GEO 报告摘要', '站点：' + label(basic['site']['name']),
             f"UTC 时间窗：{manifest['window']['start']} ≤ 时间 < {manifest['window']['end']}",
             '筛选：' + label(manifest['filters']),
             f"回答样本：{quality['samples']}；显式合成：{quality['synthetic_samples']}；未分类：{quality['unclassified_samples']}",
             f"品牌提及样本：{basic['mentioned_samples']}；站点引用率：{percent(basic['citation_rate'])}"]
    for key, title in (('without_citations', '缺引用'), ('without_evidence_url', '缺证据链接'), ('unknown_model', '未知模型')):
        m = quality[key]
        lines.append(f"{title}：{m['count']}/{m['evaluated_samples']}，{percent(m['rate'])}；字段未知：{m['unknown_samples']}")
    lines += ['校验通过仅表示包内摘要及已检查统计一致，不证明来源真实或内容正确。',
              '未分类不等于真实样本；合成样本不代表实际曝光；链接存在不证明证据有效。',
              '当前保留样本的固定快照；不构成全市场曝光率或综合 GEO 评分。']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file')
    parser.add_argument('--summary', action='store_true')
    args = parser.parse_args()
    try:
        checked = verify(args.file, Limits.from_env().response_bytes)
    except (OSError, ValueError):
        parser.exit(1, 'Report bundle validation failed.\n')
    print(summary(checked) if args.summary else json.dumps(dict(valid=True,
        version=checked['manifest']['version'], samples=checked['quality']['samples'],
        uncompressed_bytes=checked['uncompressed_bytes'])))


if __name__ == '__main__':
    main()
