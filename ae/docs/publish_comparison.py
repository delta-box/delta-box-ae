#!/usr/bin/env python3
"""Validate a selected comparison attempt and emit README image snippets.

This publication helper does not run experiments or edit the input README.
Use only after selecting the formal attempt; an output path must be new.
"""
from __future__ import annotations
import argparse
import hashlib
import html
import json
import os
from pathlib import Path
from urllib.parse import quote

ITEMS = ('table-02', 'table-03', 'figure-02', 'figure-06',
         'figure-07', 'figure-08', 'figure-09')


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def checked(path, digest):
    if not path.is_file() or sha256(path) != digest:
        raise ValueError(f'Missing or changed publication artifact: {path}')
    return path


def load(path):
    return json.loads(path.read_text())


def href(path, readme):
    return quote(os.path.relpath(path.resolve(), readme.parent.resolve()), safe='/.-_')


def bound(record, local):
    # Prefer the relocated attempt, never a surviving path to another campaign.
    return checked(local, record['sha256'])


def publish(manifest_path, readme, output, expected_source, run_root=None):
    manifest_path, readme, output = map(Path, (manifest_path, readme, output))
    if output.exists():
        raise ValueError(f'Refusing to replace an existing output: {output}')
    manifest = load(manifest_path)
    if (manifest.get('schema_version') != 1 or manifest.get('kind') != 'fresh-review-paper-comparison'
            or manifest.get('source') != 'fresh' or not manifest.get('analysis') or not manifest.get('plots')):
        raise ValueError('Publication requires a completed fresh comparison with bound analysis and plots')
    release = manifest.get('release') or {}
    if release.get('source_sha256') != expected_source or len(expected_source) != 64:
        raise ValueError('Comparison source SHA-256 does not match the selected source lock')
    attempt = manifest_path.parent.name
    if not attempt.startswith('attempt-') or not attempt[8:].isdigit():
        raise ValueError('Select comparison/attempt-NNN/manifest.json explicitly')
    root = Path(run_root) if run_root else manifest_path.parents[2]
    coverage_path = bound(manifest['coverage'], root/'coverage'/attempt/'review.json')
    analysis_path = bound(manifest['analysis'], root/'analysis'/attempt/'summary.json')
    plots_path = bound(manifest['plots'], root/'plots'/attempt/'plots.json')
    coverage, analysis, plots = map(load, (coverage_path, analysis_path, plots_path))
    if analysis.get('source') != 'fresh' or plots.get('source') != 'fresh' or plots.get('input_sha256') != sha256(analysis_path):
        raise ValueError('Plots and analysis do not describe the same fresh input')
    if (coverage.get('release') or {}).get('source_sha256') != expected_source:
        raise ValueError('Coverage snapshot belongs to another source')
    populations = analysis.get('selection', {}).get('populations', [])
    # Validate every row in the seven published entries. Other analysis entries
    # (for example correctness/suite_pass) are not inputs to these images.
    metric_rows = [row for key, experiment in analysis.get('experiments', {}).items()
                   if key in ITEMS
                   for row in experiment.get('metrics', []) + experiment.get('series', [])]
    identities = {row.get('source_identity', 'unknown') for row in metric_rows}
    if identities != {'release-sha256:' + expected_source}:
        raise ValueError('Analysis contains unknown or different measured source identities')
    for artifact in plots.get('artifacts', []):
        checked(plots_path.parent/Path(artifact['path']).name, artifact['sha256'])
    items = {item['experiment']: item for item in manifest.get('items', [])}
    # Historical comparisons may additionally contain the retired motivation figure.
    if (not set(ITEMS).issubset(items) or set(items) - set(ITEMS) - {'figure-01'}
            or len(manifest['items']) != len(items)):
        raise ValueError('Comparison must contain the seven README entries, with only optional historical Figure 1')
    local_paper = readme.parent/'ae/reference/figures'
    if not local_paper.is_dir():
        local_paper = readme.parent/'reference/figures'
    for key, item in items.items():
        checked(local_paper/(key+'.png'), item['original']['sha256'])
        for artifact in item['artifacts']:
            name = artifact['path']
            if Path(name).name != name:
                raise ValueError('Comparison artifact must be a sibling filename')
            checked(manifest_path.parent/name, artifact['sha256'])
    commit = release.get('source_commit') or 'not-recorded'
    lines = ['# 正式 AE 图片发布片段', '',
             '此文件只生成链接与图注，没有修改 README，也没有新增性能测量。', '',
             f'- 选定源码：`{commit}`；source SHA-256：`{expected_source}`。',
             f'- 选定 attempt：`{attempt}`；保留全部 {len(populations)} 个统计群组。',
             f'- 对比清单：[{manifest_path.name}]({href(manifest_path, readme)})；'
             f'覆盖快照：[review.json]({href(coverage_path, readme)})。', '',
             '下面所有链接均相对于传入的 README 所在目录。把每个 `<td>` 替换到同图号的 AE 结果单元格；论文原图保持不变。', '']
    is_zh = readme.name == 'README-zh.md'
    caption = '脚本输出示例' if is_zh else 'Example script output'
    full_comparison = '查看并排大图' if is_zh else 'Open full comparison'
    for key in ITEMS:
        item = items[key]
        title = item['paper_item'] + ('，仅 CPU' if key == 'figure-08' else '')
        png = next(artifact for artifact in item['artifacts'] if artifact['kind']=='ae' and artifact['path'].endswith('.png'))
        compare = next(artifact for artifact in item['artifacts'] if artifact['kind']=='comparison' and artifact['path'].endswith('.png'))
        image_link = href(manifest_path.parent/png['path'], readme)
        comparison_link = href(manifest_path.parent/compare['path'], readme)
        cell = (f'<td align="center"><a href="{image_link}"><img src="{image_link}" '
                f'alt="{html.escape(item["paper_item"])}: {caption}" width="300"></a><br>'
                f'<small>{caption}<br><a href="{comparison_link}">{full_comparison}</a></small></td>')
        lines += [f'## {title}', '', f'<!-- AE-RESULT:{key}:start -->', cell,
                  f'<!-- AE-RESULT:{key}:end -->', '']
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x') as stream:
        stream.write('\n'.join(lines))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--readme', type=Path, required=True, help='Link target README; remains unchanged')
    parser.add_argument('--expected-source-sha256', required=True)
    parser.add_argument('--run-root', type=Path, help='Relocated review root containing coverage/analysis/plots')
    parser.add_argument('--output', type=Path, required=True, help='New Markdown snippet file')
    args = parser.parse_args()
    try:
        result = publish(args.manifest, args.readme, args.output, args.expected_source_sha256, args.run_root)
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        parser.exit(2, f'Publication snippets not generated: {exc}\n')
    print(result)


if __name__ == '__main__':
    main()
