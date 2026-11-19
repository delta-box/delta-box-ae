#!/usr/bin/env python3
from pathlib import Path
import os,subprocess,sys
root=Path(__file__).resolve().parents[1]/'refresh-20261111'
(root/'source').mkdir(exist_ok=True)
(root/'build-info/figure7-text-spacing-20261109').mkdir(parents=True,exist_ok=True)
env=dict(os.environ,MPLBACKEND='Agg')
for name in ['plot_fig_e2b_cube_compare.py','plot_fig_end2end.py','plot_fig_rl_combo.py','plot_fig_war_2x2.py']:
 subprocess.run([sys.executable,str(root/'plot'/name)],check=True,cwd=root/'plot',env=env)
print('Figures:',root/'source')
