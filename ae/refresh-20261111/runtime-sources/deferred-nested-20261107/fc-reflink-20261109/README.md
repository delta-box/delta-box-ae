# FC-Diff CoW rerun results

| Group | Traces | CK | RS | CK ms | RS ms | Ready ms | Post-ready ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| Django | 2 | 53 | 54 | 589.448 | 2222.250 | 2222.196 | 0.055 |
| SymPy | 2 | 54 | 56 | 766.951 | 3305.236 | 3305.181 | 0.055 |
| Scientific | 5 | 130 | 140 | 672.772 | 2643.403 | 2643.340 | 0.063 |
| Tools/Small | 3 | 80 | 84 | 565.980 | 2172.777 | 2172.718 | 0.059 |
| Event Avg | 12 | 317 | 334 | 647.933 | 2567.918 | 2567.858 | 0.059 |

New CoW reconstruction cohort; continuous windows, 317 aligned CK /334 RS. Validation-only pilot is excluded. Paper unchanged.
