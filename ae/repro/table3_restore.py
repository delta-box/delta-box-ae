"""Separate Table3 slow restore windows and a hash-verified archive comparison.

Reference statistics never fill a missing fresh timer. The matched subset is
chosen by the archived input names/counts, not by current measured performance.
"""
from __future__ import annotations
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import statistics
from ae.repro.paper_tables import slow_restore_windows

KIND = 'table3-slow-restore-timing-v1'

# Published Table3 metadata from the checked-in archive. These hashes fix the
# input/count selection and result-file identities; they contain no latency.
REFERENCE_METADATA_SHA256 = {
    'table-03/files.jsonl': 'bdd52965822d1f75d911482e38cd53c6243bfa45f6606c41495163a342310286',
    'table-03/cohort-deltabox.csv': '70255ae00017673de03496c736354c5e134154ce3444d0e4ce0994d16ad03e50',
}

def unavailable(reason, **details):
    return dict(status='unavailable', reason=reason, **details)

def valid_value(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0

def load_reference(paper_root):
    """Read only the declared Table3 archive; never substitute printed constants."""
    root = Path(paper_root).absolute()
    manifest = root/'table-03/files.jsonl'
    cohort = root/'table-03/cohort-deltabox.csv'
    if not manifest.is_file() or not cohort.is_file():
        return unavailable('Table3 archive manifest/cohort is unavailable.')
    from ae.repro.analysis import Evidence, select_delta
    ev = Evidence(root, 'table3-reference')
    ev.expected.update(REFERENCE_METADATA_SHA256)
    records = ev.jsonl(manifest, allow_empty=True)
    expected = set()
    for record in records:
        target = Path(record['target'])
        if target.parts[:2] != ('paper', 'table-03') or '..' in target.parts:
            raise ValueError('Invalid Table3 reference manifest target')
        rel = Path(*target.parts[1:])
        if str(rel) in ev.expected:
            raise ValueError('Duplicate Table3 reference manifest target')
        ev.expected[str(rel)] = record['sha256']
        if str(rel).startswith('table-03/data/records/deltabox-slow/results/') and rel.name.endswith('.results.jsonl'):
            expected.add(root/rel)
    if not expected:
        return unavailable('Table3 archive has no declared slow result files.')
    actual = set(ev.glob('table-03/data/records/deltabox-slow/results/**/*.results.jsonl'))
    if expected-actual:
        return unavailable('Table3 reference bundle is incomplete; no partial reference is substituted.',
                           missing_files=sorted(str(p) for p in expected-actual))
    if actual-expected:
        raise ValueError('Unmanifested Table3 reference result file')
    selected, selection = select_delta(ev, 'table-03', 'slow', allow_missing=True)
    if not selected:
        return unavailable('No complete archived slow inputs.', selection=selection)
    inputs, all_values = [], []
    cleanup_missing = 0
    for run in selected:
        rows = [r for r in run['rows'] if r.get('kind') == 'restore']
        if not rows or any(r.get('restore_critical_ms') is None for r in rows):
            return unavailable('Archived critical field is missing; no wall/API fallback.', selection=selection)
        if any(not valid_value(r['restore_critical_ms']) or not valid_value(r.get('restore_wall_ms')) for r in rows):
            raise ValueError('Invalid archived Table3 restore timer')
        if any(r['restore_wall_ms'] != r['restore_critical_ms'] for r in rows):
            return unavailable('Archived wall and critical fields differ; their mapping is not established.', selection=selection)
        values = [r['restore_critical_ms'] for r in rows]
        all_values.extend(values)
        cleanup_missing += sum(r.get('restore_cleanup_ms') is None for r in rows)
        inputs.append(dict(instance=run['instance'], n=len(values), value=statistics.fmean(values),
                           source=dict(ev.sources[run['path']], path=str(ev.root/run['path']))))
    sources = []
    for rel, record in sorted(ev.sources.items()):
        path = ev.root/rel
        raw = path.read_bytes()
        if len(raw) != record['bytes'] or hashlib.sha256(raw).hexdigest() != record['sha256']:
            raise ValueError('Table3 reference changed while reading: '+rel)
        sources.append(dict(record, path=str(path)))
    return dict(status='verified', metric='restore_critical_ms', unit='ms', root=str(root),
                reference_kind='hash-verified archived slow critical field',
                value=statistics.fmean(all_values), n=len(all_values),
                input_count=len(inputs), inputs=sorted(inputs, key=lambda row: row['instance']),
                wall_equals_critical=True, cleanup_missing_events=cleanup_missing,
                selection=selection, sources=sources,
                limitation='Field/name/count comparison does not prove identical historical guest source or runtime configuration.')

def compare_windows(groups, reference):
    comparisons = []
    for group in groups:
        base = dict(population=group['population'], comparison_metric='restore_critical_ms')
        if reference.get('status') != 'verified':
            comparisons.append(dict(base, **unavailable(reference.get('reason', 'Reference unavailable.'))))
            continue
        ref_rows = reference.get('inputs', [])
        ref = {row['instance']: row for row in ref_rows}
        if (not ref or len(ref) != len(ref_rows) or any(type(r.get('n')) is not int or r['n'] <= 0
                or not valid_value(r.get('value')) for r in ref_rows)):
            raise ValueError('Invalid or duplicate Table3 reference input statistics')
        declared_n = sum(r['n'] for r in ref_rows)
        declared_mean = sum(r['value'] * r['n'] for r in ref_rows) / declared_n
        if (reference.get('n') != declared_n or not valid_value(reference.get('value'))
                or not math.isclose(reference['value'], declared_mean, rel_tol=1e-12, abs_tol=1e-12)):
            raise ValueError('Reference aggregate differs from its input statistics')
        critical = group['windows']['critical']
        source_rows = critical['sources']
        current = {row.get('instance'): row for row in source_rows}
        if critical['status'] != 'measured' or None in current or len(current) != len(source_rows):
            comparisons.append(dict(base, **unavailable('Complete per-input fresh critical timers are required.')))
            continue
        missing = sorted(set(ref)-set(current))
        if missing:
            comparisons.append(dict(base, **unavailable('The complete reference input set is not present.', missing_current_instances=missing)))
            continue
        changed_counts = [dict(instance=name, current=current[name]['n'], reference=ref[name]['n'])
                          for name in sorted(ref) if current[name]['n'] != ref[name]['n']]
        if changed_counts:
            comparisons.append(dict(base, **unavailable('Restore event counts differ for corresponding inputs.', changed_counts=changed_counts)))
            continue
        n = sum(r['n'] for r in ref_rows)
        current_sum = sum((Decimal(str(current[name]['value'])) * current[name]['n'] for name in ref), Decimal(0))
        reference_sum = sum((Decimal(str(r['value'])) * r['n'] for r in ref_rows), Decimal(0))
        if reference_sum <= 0:
            comparisons.append(dict(base, **unavailable('Reference mean is zero; relative difference is undefined.')))
            continue
        difference = (current_sum/reference_sum-1)*100
        comparisons.append(dict(base, status='compared', reference_instances=sorted(ref),
            additional_current_instances=sorted(set(current)-set(ref)),
            current=dict(value=float(current_sum/n), n=n, inputs=len(ref)),
            reference=dict(value=float(reference_sum/n), n=n, inputs=len(ref)),
            percent_change=float(difference),
            absolute_over_20_percent=abs(current_sum-reference_sum)*100 > reference_sum*20,
            aggregation='event-weighted means over the full archived name/count set; all current inputs remain in the all-input windows'))
    return comparisons

def build_timing_report(result, paper_root=None):
    groups = slow_restore_windows(result)
    if not groups:
        return None
    ready = any(g['windows']['critical']['status'] == 'measured' and g['instances'] for g in groups)
    if not ready:
        reference = unavailable('Complete per-input fresh critical timers are required before archive comparison.')
    else:
        reference = (load_reference(paper_root) if paper_root is not None
                     else unavailable('No Table3 archive reference directory was supplied.'))
    return dict(schema_version=1, kind=KIND, windows=groups, reference=reference,
                comparisons=compare_windows(groups, reference),
                notes=['All window values come from their recorded raw timer fields, never component minus daemon or API subtraction.',
                       'The restore-path daemon wait is already inside the complete component window; it is not added again.',
                       'A prestarted daemon is started in the background after its image is durable; that startup time is reported separately and is outside every restore window.',
                       'Archive values are only reference statistics; missing fresh fields remain unavailable.',
                       'A same-named critical-field comparison is not a claim of byte-identical historical deployment.'])


def timing_markdown(report, *, language='en'):
    """Render explicitly different windows; no reference value fills a fresh cell."""
    if not report:
        return []
    zh = language == 'zh'
    labels = {'critical': 'critical（原返回字段）' if zh else 'Critical return field',
              'component': '完整组件窗口' if zh else 'Complete component window',
              'api': '完整 restore API' if zh else 'Complete restore API',
              'lazy_daemon': '其中等待 lazy-pages 服务就绪' if zh else 'Included lazy-pages service wait',
              'lazy_prestart': 'lazy-pages 服务后台预启动（不在恢复路径上）' if zh else 'Lazy-pages service prestart (background, off the restore path)'}
    lines = ['### Slow restore 计时窗口' if zh else '### Slow restore timing windows', '']
    for group, comparison in zip(report['windows'], report['comparisons']):
        population = group['population']
        description = ' / '.join(str(population.get(key, 'unspecified')) for key in
                                 ('checkpoint_profile', 'run_purpose', 'source_identity'))
        lines += [('统计集合：' if zh else 'Population: ') + description,
                  ('全部输入数：' if zh else 'All input count: ') +
                  (str(group['instance_count']) if group['instance_count'] is not None else ('未记录' if zh else 'not recorded')), '',
                  '| 窗口 | 均值（ms） | 恢复事件数 |' if zh else '| Window | Mean (ms) | Restore events |',
                  '| --- | ---: | ---: |']
        for name, cell in group['windows'].items():
            value = f"{cell['value']:.6f}" if cell['status'] == 'measured' else '—'
            n = str(cell['n']) if cell['status'] == 'measured' else '—'
            lines.append('| '+labels[name]+' | '+value+' | '+n+' |')
        lines.append('')
        if comparison['status'] == 'compared':
            current, reference = comparison['current'], comparison['reference']
            if zh:
                text = (f"论文归档同名 {reference['inputs']} 条 / {reference['n']} 次的 critical 字段对照："
                        f"本次 {current['value']:.6f} ms，归档 {reference['value']:.6f} ms，"
                        f"偏差 {comparison['percent_change']:+.2f}%。")
            else:
                text = (f"Same-name archived {reference['inputs']} inputs / {reference['n']} events, critical field: "
                        f"current {current['value']:.6f} ms; archive {reference['value']:.6f} ms; "
                        f"difference {comparison['percent_change']:+.2f}%.")
            lines += [text, ('参考子集由原归档名称和事件数确定；全部新输入继续保留。' if zh else
                             'The reference subset is fixed by archived names and event counts; all new inputs remain reported.'), '']
        else:
            lines += [('归档字段对照不可用：' if zh else 'Archive-field comparison unavailable: ')+comparison['reason'], '']
    lines += [('critical、完整组件和完整 API 是不同窗口。恢复路径上等待服务就绪的时间已包含在组件窗口内，不能重复相加；critical 不通过减去这段时间推算。'
               '预启动的服务在镜像落盘后于后台启动，其启动时间单独列出，不属于任何恢复窗口。' if zh else
               'Critical, complete component, and complete API are different windows. The restore-path service wait is already inside the component window; critical is never derived by subtracting it. '
               'A prestarted service starts in the background once its image is durable; its startup time is listed separately and belongs to no restore window.'),
              ('参考值只用于单独对照，不填补本次缺失数据；同名字段比较不代表原 guest 源码和配置逐字节一致。' if zh else
               'References only support a separate comparison and never fill missing current data. Matching field names does not prove byte-identical historical guest source/configuration.'), '']
    return lines
