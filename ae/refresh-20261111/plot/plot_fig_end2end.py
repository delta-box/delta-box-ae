#!/usr/bin/env python3
"""Figure 7: LLM+action+C/R component sums; original DeltaBox inputs.

Preserves the original bar palette, line widths, and font sizes. The pale
1.0 band denotes LLM+action; the upper band denotes measured C/R work.
"""
import json
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D
from paperstyle import C, FS, style_axes, save

P = Path(__file__).resolve().parent
DATA = P/'figure7-data.json'
SYSTEMS = ['deltabox','e2b','cube']
LABEL = {'deltabox':'DeltaBox', 'e2b':'E2B (diff)', 'cube':'CubeSandbox'}
COLOR = {'deltabox':'#FFA500','e2b':'#87CEEB','cube':'#9ACD32'}
EDGE = {'deltabox':'#cc7a00','e2b':'#4a8db8','cube':'#557b13'}
HATCH = {'deltabox':'////','e2b':'xxxx','cube':'....'}

def main():
    d = json.loads(DATA.read_text())
    groups = d['groups']
    x = np.arange(len(groups))
    w = 0.25
    bar_step = 0.32
    fig, ax = plt.subplots(figsize=(3.4,1.9))
    for i,s in enumerate(SYSTEMS):
        ratios = np.array([d['data'][g][s]['ratio'] for g in groups])
        pos = x + (i-1)*bar_step
        ax.bar(pos, np.ones(len(groups)), w, color=COLOR[s], alpha=.22,
               edgecolor=EDGE[s], linewidth=.45, hatch=HATCH[s])
        ax.bar(pos, ratios-1, w, bottom=1, color=COLOR[s], alpha=.50,
               edgecolor=EDGE[s], linewidth=.7, hatch=HATCH[s])
        for xi,r in zip(pos,ratios):
            # Keep the label over its bar's outer side, clear of the taller neighbor.
            offset, align = {'deltabox': (0, 'center'),
                             'e2b': (w/2, 'right'),
                             'cube': (-w/2, 'left')}[s]
            ax.text(xi+offset,r+.04,f'{r:.2f}×',ha=align,va='bottom',
                    fontsize=FS.COL_TICK-2,color=EDGE[s],fontweight='bold')
    proxies = [mpatches.Patch(facecolor=COLOR[s],edgecolor=EDGE[s],alpha=.50,
                             hatch=HATCH[s],label=LABEL[s]) for s in SYSTEMS]
    proxies.append(Line2D([0],[0],color=C.SYS_EDGE,linestyle='--',linewidth=.8,
                         alpha=.7,label=r'1.0$\times$ = LLM+action'))
    ax.legend(handles=proxies,fontsize=FS.COL_LEGEND,
              loc='upper center',bbox_to_anchor=(.5,1.26),ncol=2,
              handlelength=1.2,handletextpad=.4,borderpad=.2,
              columnspacing=.9,labelspacing=.3)
    ax.set_xticks(x)
    ax.set_xticklabels(groups,fontsize=FS.COL_TICK)
    ax.tick_params(axis='y',labelsize=FS.COL_TICK)
    ax.set_ylabel('Time / LLM+action latency',fontsize=FS.COL_AXIS)
    ax.set_ylim(0,2.5)
    ax.set_yticks([0,.5,1,1.5,2,2.5])
    ax.axhline(1,color=C.SYS_EDGE,linestyle='--',linewidth=.6,alpha=.5,zorder=0)
    style_axes(ax,ygrid=True,xgrid=False)
    plt.tight_layout(pad=.3)
    save(fig,P.parent/'source/end2end.pdf')
    fig.savefig(P.parent/'build-info/figure7-text-spacing-20261109/figure7-updated.png',
                bbox_inches='tight',pad_inches=.02,dpi=220)

if __name__ == '__main__':
    main()
