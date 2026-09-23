"""Compact Figure 9 format with manifest-bound current measurements only."""
import math

BINS = ((1,8),(8,16),(16,32),(32,64),(64,128),(128,256))
STYLES = {
    'ext4': ('^','#FF8C00','without reflink (ext4)','--',4.5,5),
    'xfs': ('s','#6A5ACD','without reflink (xfs)','-',4,3),
    'xfs_reflink': ('d','#006400','with reflink','-',4,4),
}
RC = {'font.family':'STIXGeneral','mathtext.fontset':'stix','font.size':8,
      'pdf.fonttype':42,'ps.fonttype':42,'axes.spines.top':False,
      'axes.spines.right':False,'legend.frameon':False}


def figure9_metadata(result):
    points = []
    seen = set()
    for index,row in enumerate(result.get('series',[])):
        identity = (row['panel'],row['arm'],row['bin_lo'],row['bin_hi'])
        if identity in seen:
            raise ValueError('Conflicting Figure 9 populations for the same point')
        seen.add(identity)
        if row['n_units'] and (row['y'] is None or not math.isfinite(row['y']) or row['y'] <= 0):
            raise ValueError('Figure 9 log axes require a positive measured value')
        points.append(dict(series_index=index,**row))
    return dict(format_source='plot/plot_fig_war.py',
        format_reference='ae/docs/paper-plotting-reference.json',figsize_inches=[3.3,2.55],
        points=points,historical_shading=False,
        caption='Figure 9. Per-edit copy-up data (a) and loop-device I/O (b), grouped by original file size. Current AE measurements; two-stage medians. Sample counts and protocol are recorded in the manifest.',
        caption_bold_prefix='Figure 9.',
        savefig=dict(dpi=220,bbox_inches='tight',pad_inches=.02))


def figure9(plt,result):
    metadata = figure9_metadata(result)
    rows = metadata['points']
    with plt.rc_context(RC):
        fig,axes = plt.subplots(2,1,figsize=(3.3,2.55),sharex=True)
        centers = [(lo+hi)/2*1024 for lo,hi in BINS]
        for arm,(marker,color,label,linestyle,size,zorder) in STYLES.items():
            for panel,ax in zip(('a','b'),axes):
                measured = {(r['bin_lo'],r['bin_hi']):r for r in rows
                            if r['panel']==panel and r['arm']==arm}
                values = [measured[(lo*1024,hi*1024)]['y']
                          if (lo*1024,hi*1024) in measured and measured[(lo*1024,hi*1024)]['n_units']
                          else math.nan for lo,hi in BINS]
                if any(math.isfinite(y) for y in values):
                    ax.plot(centers,values,marker=marker,color=color,label=label,
                            linestyle=linestyle if panel=='a' else '-',markersize=size,
                            linewidth=1.25,zorder=zorder)
        for ax,title in zip(axes,('(a) Copy-up duplicated data','(b) Loop-device I/O')):
            ax.set_xscale('log');ax.set_yscale('log')
            ax.set_ylabel('bytes per edit',fontsize=8)
            ax.set_title(title,loc='left',fontsize=8,fontweight='bold',pad=3)
            ax.grid(axis='y',linestyle='--',linewidth=.4,alpha=.4)
            ax.xaxis.set_minor_locator(plt.NullLocator())
            ax.tick_params(axis='both',labelsize=7)
        axes[0].legend(fontsize=6.2,loc='upper left',borderaxespad=.15,handlelength=1.8)
        axes[1].set_xticks(centers,[f'{lo}–{hi}' for lo,hi in BINS],rotation=25,ha='right')
        axes[1].set_xlabel('Original edited-file size (KiB)',fontsize=8)
        fig.subplots_adjust(left=.17,right=.99,top=.93,bottom=.2,hspace=.45)
        return fig
