#!/usr/bin/env python3
"""Diff /proc/interrupts and /proc/softirqs CPU3 columns for fast vs slow."""
import sys, re

D = sys.argv[1] if len(sys.argv) > 1 else '/home/dong/d-overlayfs/agentfs/bench_results/diagnostic_1779532890'
CPU = 3  # cpu index

def parse_interrupts(path):
    """Return {irq_name: cpu_value} for given CPU column.
       row format:  '  8:   v0  v1 ... vN  desc_words...'
       (header already stripped, lines start with irq number/name+colon)"""
    out = {}
    with open(path) as f:
        for line in f:
            line = line.rstrip('\n')
            if not line.strip():
                continue
            # split first token (irq name) from rest
            m = re.match(r'^\s*(\S+):\s*(.*)$', line)
            if not m:
                continue
            name = m.group(1)
            rest = m.group(2)
            tokens = rest.split()
            # first 20 tokens are CPU counts (machine has 20 logical cpus)
            cnt = []
            for t in tokens[:20]:
                try:
                    cnt.append(int(t))
                except ValueError:
                    cnt.append(0)
            if len(cnt) <= CPU:
                continue
            desc = ' '.join(tokens[20:]) if len(tokens) > 20 else ''
            out[name] = (cnt[CPU], desc)
    return out

def parse_softirqs(path):
    """row: '          HI:     113379      52033 ... '  no description"""
    out = {}
    with open(path) as f:
        for line in f:
            line = line.rstrip('\n')
            if not line.strip(): continue
            m = re.match(r'^\s*(\S+):\s*(.*)$', line)
            if not m: continue
            name = m.group(1)
            tokens = m.group(2).split()
            cnt = []
            for t in tokens[:20]:
                try: cnt.append(int(t))
                except ValueError: cnt.append(0)
            if len(cnt) <= CPU: continue
            out[name] = cnt[CPU]
    return out

def diff_irq(before, after):
    rows = []
    keys = sorted(set(before)|set(after))
    for k in keys:
        b_val = before.get(k, (0,''))[0]
        a_val, desc = after.get(k, (0,''))
        d = a_val - b_val
        if d != 0:
            rows.append((k, b_val, a_val, d, desc))
    rows.sort(key=lambda r: abs(r[3]), reverse=True)
    return rows

def diff_sirq(before, after):
    rows = []
    for k in sorted(set(before)|set(after)):
        d = after.get(k,0) - before.get(k,0)
        rows.append((k, before.get(k,0), after.get(k,0), d))
    rows.sort(key=lambda r: abs(r[3]), reverse=True)
    return rows

# ---- run for fast and slow ----
def report(label):
    pre  = f"{D}/{label}/int_before.txt"
    post = f"{D}/{label}/int_after.txt"
    pre_s  = f"{D}/{label}/sirq_before.txt"
    post_s = f"{D}/{label}/sirq_after.txt"

    print(f"\n========== {label.upper()} ==========")
    print(f"\n--- /proc/interrupts CPU{CPU} delta (non-zero) ---")
    rows = diff_irq(parse_interrupts(pre), parse_interrupts(post))
    print(f"{'IRQ':<10} {'before':>12} {'after':>12} {'delta':>12}  desc")
    for r in rows[:25]:
        print(f"{r[0]:<10} {r[1]:>12} {r[2]:>12} {r[3]:>12}  {r[4]}")

    print(f"\n--- /proc/softirqs CPU{CPU} delta ---")
    rows = diff_sirq(parse_softirqs(pre_s), parse_softirqs(post_s))
    print(f"{'TYPE':<10} {'before':>12} {'after':>12} {'delta':>12}")
    for r in rows:
        if r[3] != 0:
            print(f"{r[0]:<10} {r[1]:>12} {r[2]:>12} {r[3]:>12}")

    # /proc/stat cpu3 jiffies
    sb = open(f"{D}/{label}/stat_before.txt").read().split()
    sa = open(f"{D}/{label}/stat_after.txt").read().split()
    fields = ['user','nice','sys','idle','iowait','irq','softirq','steal','guest','guest_nice']
    print(f"\n--- /proc/stat cpu{CPU} jiffies (1 jiffy = ~10 ms on HZ=100) ---")
    print(f"{'field':<10} {'before':>14} {'after':>14} {'delta':>10}")
    for i,n in enumerate(fields):
        bv, av = int(sb[i+1]), int(sa[i+1])
        if av-bv != 0:
            print(f"{n:<10} {bv:>14} {av:>14} {av-bv:>10}")

report('fast')
report('slow')

# side-by-side delta diff
print("\n========== fast vs slow delta-of-delta (CPU{}) ==========".format(CPU))
def get_deltas(label, fn_parse, prefix):
    pre = fn_parse(f"{D}/{label}/{prefix}_before.txt")
    post = fn_parse(f"{D}/{label}/{prefix}_after.txt")
    out = {}
    for k in set(pre)|set(post):
        if fn_parse is parse_interrupts:
            b = pre.get(k,(0,''))[0]; a = post.get(k,(0,''))[0]
        else:
            b = pre.get(k,0); a = post.get(k,0)
        out[k] = a-b
    return out

fast_int  = get_deltas('fast', parse_interrupts, 'int')
slow_int  = get_deltas('slow', parse_interrupts, 'int')
fast_sirq = get_deltas('fast', parse_softirqs, 'sirq')
slow_sirq = get_deltas('slow', parse_softirqs, 'sirq')

print(f"\n--- /proc/interrupts CPU{CPU}: slow-delta MINUS fast-delta ---")
diffs = []
for k in set(fast_int)|set(slow_int):
    d = slow_int.get(k,0) - fast_int.get(k,0)
    if d != 0:
        diffs.append((k, fast_int.get(k,0), slow_int.get(k,0), d))
diffs.sort(key=lambda r: abs(r[3]), reverse=True)
print(f"{'IRQ':<10} {'fast Δ':>12} {'slow Δ':>12} {'slow-fast':>12}")
for r in diffs[:25]:
    print(f"{r[0]:<10} {r[1]:>12} {r[2]:>12} {r[3]:>12}")

print(f"\n--- /proc/softirqs CPU{CPU}: slow-delta MINUS fast-delta ---")
diffs = []
for k in set(fast_sirq)|set(slow_sirq):
    d = slow_sirq.get(k,0) - fast_sirq.get(k,0)
    diffs.append((k, fast_sirq.get(k,0), slow_sirq.get(k,0), d))
diffs.sort(key=lambda r: abs(r[3]), reverse=True)
print(f"{'TYPE':<10} {'fast Δ':>12} {'slow Δ':>12} {'slow-fast':>12}")
for r in diffs:
    if r[3] != 0:
        print(f"{r[0]:<10} {r[1]:>12} {r[2]:>12} {r[3]:>12}")
