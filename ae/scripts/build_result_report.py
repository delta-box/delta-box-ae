#!/usr/bin/env python3
"""Render the checked-in paper/result report from hash-locked analysis snapshots.

This does not run experiments or turn incomplete populations into paper cohorts.
Re-analyzing raw runs uses reproduce.py analyze, which validates their artifacts.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ae.repro.plot import COLORS, LABELS, render


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_inputs(report):
    lock = json.loads((report / 'inputs.json').read_text())
    data = {}
    for key, record in lock['inputs'].items():
        path = (report / record['path']).resolve()
        path.relative_to(report.resolve())
        if path.stat().st_size != record['bytes'] or digest(path) != record['sha256']:
            raise ValueError(f'Changed report input: {key}')
        data[key] = json.loads(path.read_text())
        if key != 'published-table-02' and data[key].get('source') != record['source']:
            raise ValueError(f'Wrong evidence class: {key}')
    paper = ROOT / 'ae/reference/paper158.pdf'
    if digest(paper) != data['published-table-02']['paper_sha256']:
        raise ValueError('Published numbers no longer match the pinned paper')
    return data, lock


def experiment(data, source, key, field):
    return data[source]['experiments'][key][field]


def one(rows, **filters):
    selected = [r for r in rows if all(r.get(k) == v for k, v in filters.items())]
    if len(selected) != 1:
        raise ValueError(f'Expected exactly one observation: {filters}; found {len(selected)}')
    return selected[0]


def finish(fig, output, name, title, note):
    fig.suptitle(title, x=.055, ha='left', fontsize=16, weight='bold')
    note += '\nFresh validation: CPU/NUMA not pinned; no fixed-frequency protocol or effective-frequency time series.'
    fig.text(.055, .022, note, fontsize=9, color='#525f69', va='bottom')
    fig.subplots_adjust(top=.83, bottom=.20, left=.09, right=.97, wspace=.27, hspace=.52)
    for suffix in ('png', 'pdf'):
        fig.savefig(output / f'{name}.{suffix}', dpi=180, bbox_inches='tight',
                    metadata={'Creator': 'DeltaBox result report'} if suffix == 'pdf' else None)


def table2(plt, data, output):
    backends = ['replay', 'fc-diff', 'criu', 'cube', 'e2b', 'deltabox']
    archive = experiment(data, 'archived', 'table-02', 'metrics')
    fresh = experiment(data, 'validation-002', 'table-02', 'metrics')
    published = data['published-table-02']['values']
    fig, axes = plt.subplots(2, 2, figsize=(12, 8.5))
    rows = []
    for col, metric in enumerate(('checkpoint_ms', 'restore_ms')):
        ax = axes[0, col]
        old = [one(archive, backend=b, group='All', metric=metric) for b in backends]
        ax.bar([i-.18 for i in range(6)], [published[b][metric] for b in backends], .36,
               color='#263d52', label='Published Table 2: Event Avg')
        ax.bar([i+.18 for i in range(6)], [r['value'] for r in old], .36,
               color='#78a6b8', label='Recovered archive: event mean')
        ax.set_xticks(range(6), ['Replay', 'FC-Diff', 'CRIU', 'Cube', 'E2B', 'DeltaBox'], fontsize=9)
        ax.set_yscale('log')
        ax.set_ylabel('Milliseconds (log scale)')
        ax.set_title(metric.replace('_ms', '').capitalize() + ' — published vs recovered', loc='left')
        if col == 0:
            ax.legend(fontsize=8)
        for b, r in zip(backends, old):
            rows.append(dict(backend=b, metric=metric, published_ms=published[b][metric],
                             archived_ms=r['value'], archived_events=r['n'], archived_traces=r['n_traces']))
        ax = axes[1, col]
        for i, b in enumerate(('replay', 'deltabox')):
            r = one(fresh, backend='replay-zero-llm' if b == 'replay' else b, group='All', metric=metric)
            ax.bar(i, r['value'], width=.45, color=COLORS[b])
            ax.annotate(f"{r['value']:.2f} ms\nn={r['n']} events", (i, r['value']),
                        xytext=(0, 6), textcoords='offset points', ha='center', fontsize=9)
        ax.set_xticks([0, 1], ['Replay / Astropy-14309', 'DeltaBox / Django-14997'], fontsize=9)
        ax.set_yscale('log')
        ax.set_ylabel('Milliseconds (log scale)')
        ax.margins(y=.45)
        ax.set_title('Fresh prefix only — different instances and timing boundaries', loc='left', fontsize=10)
    finish(fig, output, 'table-02-comparison', 'Table 2 | Published values, recovered records, fresh prefixes',
           'Top: each backend retains its own historical cohort. DeltaBox archive differs from publication.\n'
           'Bottom: API latency for DeltaBox; measured zero-LLM Replay restore. Not an apples-to-apples performance regression test.')
    with (output / 'table-02-comparison.csv').open('w', newline='') as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]), lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)
    plt.close(fig)


def filesystem(plt, data, output):
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.5))
    factors = {'MB': 1e6, 'bytes': 1, 'KB_historical': 1000, 'KiB': 1024}
    for source, offset, color, label in [('archived', -.18, '#78a6b8', 'Archive: 30 instances'),
                                         ('validation-001', .18, '#176b55', 'Fresh: Astropy-6938 only')]:
        metrics = experiment(data, source, 'figure-02', 'metrics')
        values = [one(metrics, domain='filesystem', metric=m) for m in ('total', 'step_delta')]
        axes[0].bar([offset, 1+offset], [r['value']*factors[r['unit']] for r in values], .36,
                    color=color, label=label)
        series = [r for r in experiment(data, source, 'figure-02', 'series') if r['domain'] == 'filesystem']
        axes[1].plot([r['x'] for r in series], [r['y']*factors[r['unit']] for r in series],
                     marker='.', color=color, label=label)
    axes[0].set_xticks([0, 1], ['Repository size', 'Positive-write step delta'])
    axes[0].set_yscale('log')
    axes[0].set_ylabel('Bytes (log scale)')
    axes[0].legend(fontsize=9)
    axes[1].set_xlabel('Step index within each trace')
    axes[1].set_ylabel('Mean bytes written; zero-write steps included')
    axes[0].set_title('Figure 2(a): filesystem part', loc='left')
    axes[1].set_title('Figure 2(b): filesystem part', loc='left')
    finish(fig, output, 'figure-02-comparison', 'Figure 2 | Filesystem state and per-step changes',
           'Different populations: 30 historical inputs versus one complete fresh trace. Fresh memory panel is unavailable.\n'
           'Historical KB follows its recorded 1000-byte display convention; fresh KiB is converted with 1024.')
    plt.close(fig)


def memory(plt, data, output):
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.5))
    for ax, source, title in zip(axes, ('archived', 'memory-001'),
                                 ('Archived: 28 checkpoints per policy', 'Fresh prefix: 2 checkpoints per policy')):
        rows = experiment(data, source, 'figure-06', 'series')
        for arm in ('none', 'skip', 'gc', 'warm'):
            selected = sorted((r for r in rows if r['panel'] == 'a' and r['arm'] == arm), key=lambda r:r['x'])
            ax.plot([r['x'] for r in selected], [r['y'] for r in selected], marker='.',
                    color=COLORS[arm], label=LABELS[arm])
        ax.set_title(title, loc='left')
        ax.set_xlabel('Checkpoint observation index')
        ax.set_ylabel('tmpfs + template PSS + active PSS (MiB)')
        ax.legend(fontsize=8)
    axes[1].set_xticks([1, 2], ['1 (bootstrap)', '2'])
    finish(fig, output, 'figure-06a-comparison', 'Figure 6(a) | Retention policies on SymPy-22840',
           'Separate x/y ranges: the fresh prefix includes bootstrap; observations are not matched deep-search states.\n'
           'Both runs are fork-only (CRIU disabled). Historical “MB” is MiB in the pinned original aggregation code.')
    plt.close(fig)


def adaptive(plt, data, output):
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.5))
    for ax, source, title in zip(axes, ('archived', 'validation-001'),
                                 ('Archived: 12 inputs', 'Fresh: Django-12143 prefix')):
        rows = experiment(data, source, 'figure-06', 'series')
        for arm, color, label in [('standard_only', '#7e6298', 'Standard-only'),
                                   ('adaptive_lightweight', '#176b55', 'Adaptive: LW'),
                                   ('adaptive_standard', '#df8c2b', 'Adaptive: standard')]:
            selected = sorted((r for r in rows if r['panel'] == 'b' and r['arm'] == arm), key=lambda r:r['bin_lo'])
            if selected:
                n = sum(r['y'] for r in selected)
                ax.stairs([r['y'] for r in selected], [r['bin_lo'] for r in selected]+[selected[-1]['bin_hi']],
                          color=color, label=f'{label} (n={int(n)})')
            else:
                ax.plot([], [], color=color, label=f'{label} (n=0)')
        ax.set_xscale('log')
        ax.set_xlim(.05, 500)
        ax.set_xlabel('Checkpoint latency (ms)')
        ax.set_ylabel('Events per recorded bin')
        ax.set_title(title, loc='left')
        ax.legend(fontsize=8)
    finish(fig, output, 'figure-06b-comparison', 'Figure 6(b) | Standard checkpoints and lightweight skip',
           'Different event counts: archive 1081 standard-only vs 831 LW + 250 standard; fresh has two events per arm.\n'
           'Fresh bootstrap is excluded. A short successful prefix does not establish full-chain correctness or paper-level speedup.')
    plt.close(fig)


def fanout(plt, data, output):
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.5))
    old = [r for r in experiment(data, 'archived', 'figure-08', 'series') if r['panel'] == 'a']
    new = [r for r in experiment(data, 'fanout-report', 'figure-08', 'series') if r['panel'] == 'a']
    for backend in ('deltabox', 'cube', 'e2b'):
        rows = sorted((r for r in old if r['backend'] == backend), key=lambda r:r['x'])
        measured = [r for r in rows if not r.get('estimated')]
        axes[0].plot([r['x'] for r in measured], [r['y'] for r in measured], marker='o',
                     color=COLORS[backend], label=LABELS[backend])
        estimated = [r for r in rows if r.get('estimated')]
        if estimated:
            tail = measured[-1:] + estimated
            axes[0].plot([r['x'] for r in tail], [r['y'] for r in tail], '--D', color=COLORS[backend],
                         markerfacecolor='white', label='E2B N64: historical estimate')
    for rows, color, label in [(old, '#78a6b8', 'Recovered DeltaBox'), (new, '#176b55', 'Fresh DeltaBox / report run')]:
        rows = sorted((r for r in rows if r['backend'] == 'deltabox'), key=lambda r:r['x'])
        axes[1].plot([r['x'] for r in rows], [r['y'] for r in rows], marker='o', color=color, label=label)
        if label.startswith('Fresh'):
            for r in rows:
                axes[1].annotate(f"{r['y']:.1f}", (r['x'],r['y']), xytext=(4,6), textcoords='offset points', fontsize=9)
    for ax, title in zip(axes, ('Historical substrate results', 'DeltaBox: same 64 MiB protocol, separate runs')):
        ax.set_xscale('log', base=2)
        ax.set_yscale('log')
        ax.set_xticks([1,4,16,64], ['1','4','16','64'])
        ax.set_xlabel('Children requested')
        ax.set_ylabel('All children ready + verified (ms)')
        ax.set_title(title, loc='left')
        ax.legend(fontsize=8)
    finish(fig, output, 'figure-08a-comparison', 'Figure 8(a) | CPU fan-out with inherited-state verification',
           'Fresh N=1/4/16/64 were all executed; this is one run, not repeated statistical validation.\n'
           'Historical E2B N64 remains an estimate. No fresh Cube/E2B result. GPU panels 8(b)/(c) skipped.')
    plt.close(fig)


def war(plt, data, output):
    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    markers = {'ext4':'^', 'xfs':'s', 'xfs_reflink':'D'}
    for col, (source, title) in enumerate([('archived', 'Archive: 185 pool+instance inputs'),
                                          ('validation-001', 'Fresh: one input, two successful edits')]):
        rows = experiment(data, source, 'figure-09', 'series')
        for row, panel in enumerate(('a','b')):
            ax=axes[row,col]
            for arm in ('ext4','xfs','xfs_reflink'):
                selected=sorted((r for r in rows if r['panel']==panel and r['arm']==arm and r['n_units'] and r['y'] is not None),key=lambda r:r['x'])
                ax.plot([r['x']/1024 for r in selected],[r['y'] for r in selected], marker=markers[arm],
                        color=COLORS[arm], label=LABELS[arm], markersize={'ext4':9,'xfs':6,'xfs_reflink':4.5}[arm],
                        markerfacecolor='none' if arm=='ext4' else COLORS[arm])
            ax.set_xscale('log',base=2)
            ax.set_yscale('log')
            ax.set_xticks([4.5,12,24,48,96,192], ['1–8','8–16','16–32','32–64','64–128','128–256'],fontsize=8)
            ax.set_xlim(3,260)
            ax.set_ylabel('Copy-up bytes' if panel=='a' else 'Physical I/O bytes')
            ax.set_title(title if row==0 else 'Figure 9(b)',loc='left')
            if row==0:ax.legend(fontsize=8)
            if row==1:ax.set_xlabel('Original file-size bin (KiB)')
    for pair in axes:
        low=min(ax.get_ylim()[0] for ax in pair);high=max(ax.get_ylim()[1] for ax in pair)
        for ax in pair:ax.set_ylim(low,high)
    finish(fig, output, 'figure-09-comparison', 'Figure 9 | Copy-up and physical writes by filesystem',
           'Shared vertical scales within each row. Empty fresh bins stay empty; no historical values are filled in.\n'
           'Fresh uses sparse 4 GiB filesystems and real Git base-file contents, not the full historical OCI image.')
    plt.close(fig)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report', type=Path, default=ROOT/'ae/report')
    p.add_argument('--output', type=Path)
    args=p.parse_args()
    data, lock=load_inputs(args.report)
    output=(args.output or args.report/'figures').resolve()
    output.mkdir(parents=True, exist_ok=True)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.titlesize':11,
                         'axes.spines.top':False,'axes.spines.right':False,'pdf.fonttype':42})
    archive_manifest=render(args.report/'data/archived.json', output/'archived')
    for draw in (table2,filesystem,memory,adaptive,fanout,war):
        draw(plt,data,output)
    names=('table-02-comparison','figure-02-comparison','figure-06a-comparison',
           'figure-06b-comparison','figure-08a-comparison','figure-09-comparison')
    paths=[output/f'{name}.{suffix}' for name in names for suffix in ('png','pdf')]
    paths += [Path(item['path']) for item in archive_manifest['artifacts']]
    paths += [output/'archived/plots.json', output/'table-02-comparison.csv']
    artifacts=[{'path':str(p.relative_to(output)), 'sha256':digest(p), 'bytes':p.stat().st_size}
               for p in sorted(paths)]
    manifest={'schema_version':1,'inputs':lock['inputs'],'artifacts':artifacts,
              'scope':'Recovered archive and explicitly partial fresh runs; not full paper reproduction.',
              'fresh_paper_cohort_verified':False,'archive_plot_count':len(archive_manifest['artifacts'])}
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(f'Rendered {sum(p["path"].endswith((".png",".pdf")) for p in artifacts)} plots: {output}')


if __name__=='__main__':
    main()
