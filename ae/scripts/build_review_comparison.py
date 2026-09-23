#!/usr/bin/env python3
"""Place hash-verified original paper crops beside this campaign's fresh plots.

This presentation step never reads archived measurements or fills missing values.
Paper tables and Figures 2, 6 and 7 place independently identified populations
in the paper's fixed domains, panels, columns or bars.
With no fresh analysis, omit --analysis and --plots for missing-result panels.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil

AE = Path(__file__).resolve().parents[1]
PAPER_LAYOUT_KEYS = frozenset(('table-02', 'table-03', 'figure-02', 'figure-06', 'figure-07', 'figure-09'))
PAPER_SHA256 = "1cc012a6ba4afdd127372a236333983ad6d17934ac63680b37faa3e6f65bb95e"
ITEMS = {
    "table-02": ("Table 2", ("table-02-",), "Full controller API event means; separate backend cohorts. Paper critical timers can differ."),
    "table-03": ("Table 3", ("table-02-deltabox", "table-03-slow"), "Measured internal timers only. Overlapping timer windows must not be added."),
    "figure-02": ("Figure 2", ("figure-02-",), "Positive filesystem writes for delta bars; actual contributors per step; binary KiB/MiB."),
    "figure-06": ("Figure 6", ("figure-06-",), "Memory-policy and adaptive populations remain separate; missing arms stay missing."),
    "figure-07": ("Figure 7", ("table-02-deltabox", "table-02-e2b"), "Derived serialized component model, NOT measured end-to-end latency or async overlap."),
    "figure-08": ("Figure 8(a)", ("figure-08-",), "CPU fan-out: children ready and verified. The full one-click run also measures panel (b) on GPUs and calculates panel (c) from those timings."),
    "figure-09": ("Figure 9", ("figure-09",), "Two-stage medians over input-file units; no historical shading or missing-arm backfill."),
}


BOUNDARIES_ZH = {
    "table-02": "完整 controller API 的事件平均耗时；各 backend 的输入集合独立统计。论文的关键路径计时范围可能不同。",
    "table-03": "仅展示实测的内部计时。存在重叠的计时窗口不能相加。",
    "figure-02": "文件系统增量柱统计正写入步骤；每步按实际贡献的输入统计；输入中的 KiB/MiB 为二进制单位。",
    "figure-06": "内存策略与 adaptive 的统计集合分别保留；缺失的实验臂不补值。",
    "figure-07": "由串行计时组件推导的模型，不是实测端到端时延或异步重叠执行时间。",
    "figure-08": "CPU fan-out：子实例就绪并通过验证。完整一键运行同时测量面板 (b) 的 GPU 时延，并据此计算面板 (c)。",
    "figure-09": "按输入与文件组成的单位进行两阶段中位数聚合；不沿用历史阴影区域，也不为缺失实验臂补值。",
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def coverage_rows(coverage):
    """Accept both the one-click review manifest and the older explicit audit schema."""
    rows = coverage.get("coverage", coverage.get("rows", []))
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Coverage must contain a coverage or rows list of objects")
    return rows


def source_identity(coverage):
    release = coverage.get("release") or coverage.get("source") or {}
    release = release if isinstance(release, dict) else {}
    return {key: release.get(key, coverage.get(key)) for key in ("source_commit", "source_sha256")}


def matching_coverage(key, coverage):
    prefixes = ITEMS[key][1]
    return [row for row in coverage_rows(coverage)
            if any(str(row.get("experiment", "")).startswith(prefix) for prefix in prefixes)]


def verify_inputs(analysis_path, plots_path, coverage_path, paper_dir):
    """Validate provenance before creating any output files, including relocated plots."""
    if bool(analysis_path) != bool(plots_path):
        raise ValueError("--analysis and --plots must be supplied together")
    coverage = load(coverage_path)
    coverage_rows(coverage)
    summary, plots = None, None
    resolved_plots = []
    if analysis_path:
        summary, plots = load(analysis_path), load(plots_path)
        if summary.get("schema_version") != 1 or summary.get("source") != "fresh":
            raise ValueError("Only canonical schema_version=1 source=fresh analysis is accepted")
        if plots.get("schema_version") != 1 or plots.get("source") != "fresh":
            raise ValueError("Only canonical schema_version=1 source=fresh plots are accepted")
        if plots.get("input_sha256") != digest(analysis_path):
            raise ValueError("Plots do not belong to the supplied fresh summary (SHA-256 mismatch)")
        seen = set()
        represented_groups = {}
        for artifact in plots.get("artifacts", []):
            if Path(artifact["path"]).suffix.lower() != ".png":
                continue
            key = artifact["experiment"]
            if key not in ITEMS:
                continue
            groups = artifact.get('populations')
            if artifact.get('layout') == 'paper':
                if (key not in PAPER_LAYOUT_KEYS or not isinstance(groups, list) or not groups
                        or any(not isinstance(group, str) for group in groups)
                        or len(set(groups)) != len(groups)):
                    raise ValueError('Invalid paper-layout populations')
                identity = (key, tuple(sorted(groups)))
            else:
                if groups is not None:
                    raise ValueError('Multiple populations require an explicit paper layout')
                identity = (key, artifact.get("population"))
            if identity in seen:
                raise ValueError(f"Duplicate plot population: {identity}")
            seen.add(identity)
            # A copied report commonly retains the producer's absolute path.
            # Prefer the copied sibling, but require the recorded exact bytes.
            sibling = Path(plots_path).parent / Path(artifact["path"]).name
            path = sibling if sibling.exists() else Path(artifact["path"])
            if digest(path) != artifact.get("sha256"):
                raise ValueError(f"Fresh plot changed after rendering: {path}")
            result = summary.get("experiments", {}).get(key, {})
            rows = result.get("metrics", []) + result.get("series", [])
            field = ("run_purpose" if key == "figure-08" and not any(
                "plot_group" in row for row in result.get("metrics", [])+result.get("series", [])) else "plot_group")
            population = artifact.get("population")
            expected = {row.get(field, "") for row in rows}
            if not rows or (set(groups) != expected if groups is not None else population not in expected):
                raise ValueError(f"Plot population is absent from fresh analysis: {identity}")
            included = set(groups) if groups is not None else {population}
            represented = represented_groups.setdefault(key, set())
            if represented & included:
                raise ValueError(f'Duplicate plot population overlaps another image: {key}')
            represented.update(included)
            resolved_plots.append(dict(artifact, resolved_path=str(path.resolve())))
    paper_dir = Path(paper_dir)
    paper = load(paper_dir / "manifest.json")
    if paper.get("source_sha256") != PAPER_SHA256:
        raise ValueError("Paper crop manifest is not bound to the supplied ATC paper")
    references = {}
    for key in ITEMS:
        record = next((row for row in paper.get("artifacts", []) if row.get("file") == key + ".png"), None)
        if not record or digest(paper_dir / record["file"]) != record.get("sha256"):
            raise ValueError(f"Missing or changed original paper crop: {key}")
        references[key] = dict(record, path=str((paper_dir / record["file"]).resolve()))
    return summary, resolved_plots, coverage, paper, references


def sample_records(key, result, population):
    """Keep n's attached to their own statistic; never add overlapping denominators."""
    field = ("run_purpose" if key == "figure-08" and not any(
                "plot_group" in row for row in result.get("metrics", [])+result.get("series", [])) else "plot_group")
    selected = set(population) if isinstance(population, list) else {population}
    rows = []
    for category in ("metrics", "series"):
        for row in result.get(category, []):
            if row.get(field, "") not in selected:
                continue
            identity = {name: row[name] for name in
                        ("backend", "mode", "checkpoint_profile", "run_purpose", "baseline_test_runtime",
                         "cohort", "instance", "group", "domain", "panel", "arm", "operation", "metric", "x",
                         "unit", "n", "n_units", "n_edits", "evidence_kind", "modeled",
                         "plot_group", "source_identity") if name in row}
            rows.append(dict(row_type=category, **identity))
    return rows


def sample_lines(key, records):
    """Show per-statistic counts/ranges without treating repeated metric n as new data."""
    timing_labels = {
        "wall_s": "total duration (s)",
        "wall_ms": "duration (ms)",
        "ckpt_wall_ms": "checkpoint duration (ms)",
        "checkpoint_wall_ms": "checkpoint duration (ms)",
        "restore_wall_ms": "restore duration (ms)",
        "checkpoint_api_wall_ms": "checkpoint API latency (ms)",
        "restore_api_wall_ms": "restore API latency (ms)",
        "restore_raw_api_wall_ms": "raw restore API duration (ms)",
        "sleep_wall_s": "measured sleep duration (s)",
    }
    grouped = {}
    for row in records:
        if row["row_type"] == "metrics":
            names = ("backend", "group", "domain", "instance", "panel", "arm", "operation", "metric")
        else:
            names = ("backend", "domain", "panel", "arm", "metric")
        label = "/".join(str(timing_labels.get(row[name], row[name]) if name == "metric" else row[name])
                         for name in names if row.get(name) is not None)
        for count_name in ("n", "n_units", "n_edits"):
            if count_name in row:
                grouped.setdefault((label, count_name), []).append(row[count_name])
    lines = []
    for (label, count_name), values in sorted(grouped.items()):
        lo, hi = min(values), max(values)
        count = str(lo) if lo == hi else f"{lo}-{hi} per point"
        lines.append(f"{label}: {count_name}={count}")
    if not lines:
        lines = ["Sample counts unavailable; no counts inferred."]
    if len(lines) > 8:
        lines = lines[:8] + [f"+ {len(lines)-8} count records; complete per-statistic counts in manifest.json"]
    return lines


def brief_population(population):
    values = re.findall(r'(?:^|;)(experiment|mode|checkpoint_profile|run_purpose|message_policy|baseline_test_runtime|mock_latency_policy|replay_timing_method|legacy_timing_policy|source_identity)=([^;]+)', population or "")
    values = [(key, re.sub(r'[0-9a-f]{40,64}', lambda match: match.group(0)[:12], value)
               if key == 'source_identity' else value) for key, value in values]
    return timing_text("; ".join(f"{key}={value}" for key, value in values if value != "null") or (population or "unspecified population"))


def timing_text(value):
    """Use readable timing labels while retaining original evidence identities."""
    text = str(value)
    replacements = (
        (r"\bsmoke\b", "quick check"),
        (r"\bzero-latency-wall\b", "zero LLM delay"),
        (r"\brecorded-wall\b", "recorded intervals"),
        (r"\bAPI wall[- ]clock interval\b", "API request timestamps"),
        (r"\bAPI wall timer\b", "measured API duration"),
        (r"\bAPI wall(?:[- ]time)?\b(?![-_])", "API latency"),
        (r"\bend-to-end wall[- ]time\b", "end-to-end latency"),
        (r"\braw wall\b", "raw elapsed time"),
        (r"\bwall[- ]time\b", "elapsed time"),
        (r"\bwall[- ]clock\b", "system clock"),
        (r"(?<![\w-])wall(?![\w-])", "duration"),
        (r"墙钟时间|墙钟", "耗时"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def missing_details(result):
    missing = {name: result[name] for name in ("missing_backends", "missing") if result.get(name)}
    panels = {}
    for name, panel in result.get("panels", {}).items():
        status = panel.get("status") if isinstance(panel, dict) else panel
        if status not in ("analyzed", "ok", "derived_model"):
            panels[name] = panel
    if panels:
        missing["panels"] = panels
    return missing


def coverage_lines(rows, *, language="en"):
    zh = language == "zh"
    statuses = {"ok": "成功", "partial": "部分完成", "failed": "失败",
                "unavailable": "不可用", "running": "运行中", "interrupted": "已中断",
                "not-run": "未运行", "pending": "待运行", "unknown": "未知"}
    lines = []
    for row in rows:
        counts = []
        for label, keys in ((("计划" if zh else "planned"), ("planned_jobs", "expected_unique_jobs")),
                            (("成功" if zh else "passed"), ("successful_jobs", "passed_unique_jobs")),
                            (("失败" if zh else "failed"), ("failed_jobs", "failed_unique_jobs"))):
            value = next((row[k] for k in keys if k in row), None)
            if value is not None:
                counts.append(f"{label}={len(value) if isinstance(value, list) else value}")
        status = row.get("status", "unknown")
        if zh and status in statuses:
            status = f"{statuses[status]}（{status}）"
        lines.append(f"{row.get('experiment', '?')}: {status}" +
                     ("; " + ", ".join(counts) if counts else ""))
        reasons = row.get("reasons", []) + row.get("prerequisite_failures", []) + row.get("notes", [])
        if reasons:
            lines.append("  " + "; ".join(str(reason) for reason in reasons))
        for arm in row.get("unavailable_arms", []):
            label = "不可用实验臂" if zh else "Unavailable arm"
            fallback = "详见运行记录" if zh else "See coverage manifest"
            lines.append(f"  {label} {arm.get('arm', '?')}: {arm.get('reason', fallback)}")
    fallback = ("没有对应的运行记录，尚未核验完整的论文输入集合。" if zh else
                "No matching coverage record; full paper cohort has not been verified.")
    return [timing_text(line) for line in lines] or [fallback]


def figure08_supplement(path, *, expected_release=None):
    """Verify optional one-click GPU outputs before linking or copying them."""
    if path is None:
        return None
    value = load(path)
    if value.get("schema_version") != 1 or not isinstance(value.get("panels"), list):
        raise ValueError("Invalid Figure 8 supplement")
    if expected_release is not None and any(
            (value.get("release") or {}).get(key) != expected_release.get(key)
            for key in ("source_commit", "source_sha256")):
        raise ValueError("Figure 8 supplement belongs to another measurement source")
    seen = set()
    for panel in value["panels"]:
        key = panel["experiment"]
        if key not in ("figure-08-gpu", "figure-08-theory") or key in seen:
            raise ValueError("Unknown or repeated Figure 8 supplemental panel")
        seen.add(key)
        stem = "figure-08b" if key == "figure-08-gpu" else "figure-08c"
        artifacts = panel.get("artifacts", [])
        expected = {stem + suffix for suffix in (".png", ".pdf")}
        if (panel["status"] == "ok" or artifacts) and {Path(item["path"]).name for item in artifacts} != expected:
            raise ValueError("Successful Figure 8 panel must include PNG and PDF")
        for record in [*artifacts, *([panel["input"]] if panel.get("input") else [])]:
            source = Path(record["path"])
            if digest(source) != record["sha256"] or source.stat().st_size != record["bytes"]:
                raise ValueError("Changed Figure 8 supplemental artifact")
        if len(artifacts) != len({Path(item["path"]).name for item in artifacts}) or any(
                Path(item["path"]).name not in expected for item in artifacts):
            raise ValueError("Invalid Figure 8 artifact filename")
    return value


def supplemental_markdown(panels, *, language):
    zh = language == "zh"
    lines = []
    for panel in panels:
        title = panel["title"]
        lines += [f"### {title}", ""]
        if panel["status"] in ("ok", "partial") and panel.get("artifacts"):
            artifacts = panel["artifacts"]
            png = next(item["path"] for item in artifacts if item["path"].endswith(".png"))
            pdf = next(item["path"] for item in artifacts if item["path"].endswith(".pdf"))
            alt = "本次运行结果" if zh else "This run's result"
            lines += [f"![{title}: {alt}]({png})", "", f"[PDF]({pdf})", ""]
            if panel["experiment"] == "figure-08-theory":
                lines += [("由本次 CPU fan-out 和 GPU 时延计算的理论占用率与 staleness。" if zh else
                           "Modeled occupation and staleness calculated from this run's CPU fan-out and GPU timings."), ""]
        else:
            lines += [("本项未成功生成结果；请查看运行日志。GPU 资源问题请联系作者。" if zh else
                       "This item did not produce a successful result; inspect its logs. Contact the authors for GPU resources."), ""]
        lines += ["- " + reason for reason in panel.get("reasons", [])]
        lines.append("")
    return lines


def markdown_index(manifest, *, language):
    """Render both reviewer pages from the same comparison manifest."""
    if language not in ("en", "zh"):
        raise ValueError("Unsupported comparison page language: " + language)
    zh = language == "zh"
    identity = manifest["release"]
    commit, source_hash = identity.get("source_commit"), identity.get("source_sha256")
    version = ((f"源码提交：`{commit[:12]}`" if commit else "源码版本未记录") if zh else
               (f"Source commit: `{commit[:12]}`" if commit else "Source version unavailable"))
    if source_hash:
        version += f"; SHA-256: `{source_hash[:16]}`"
    lines = [
        "# 本次 AE 结果与论文对比" if zh else "# This run's AE results and paper comparisons",
        "",
        "[English](README.md) | **简体中文**" if zh else "**English** | [简体中文](README-zh.md)",
        "",
        ("每幅并排图的左栏为论文原图，右栏为本次运行生成的结果。"
         "缺测项显示原因，不使用历史结果补齐。" if zh else
         "Each comparison shows the original paper on the left and results generated by this run on the right. "
         "Missing measurements show their reasons; archived results are never substituted."),
        ("不同配置、模式和运行范围分别统计。原始诊断消息保留输出时的语言；"
         "完整统计记录保存在 `manifest.json`。" if zh else
         "Configurations, modes, and run scopes are analyzed separately. Raw diagnostic messages retain their "
         "original language; complete statistical records are retained in `manifest.json`."),
        "", version, "",
    ]
    for item in manifest["items"]:
        title, key = item["paper_item"], item["experiment"]
        def artifact(kind, suffix):
            return next(row["path"] for row in item["artifacts"]
                        if row["kind"] == kind and row["path"].endswith(suffix))
        alt = f"{title}：论文原图与本次结果" if zh else f"{title}: paper and this run"
        ae_label = "本次结果图片" if zh else "This run's plot"
        pdf_label = "完整对比 PDF" if zh else "Full comparison PDF"
        lines += [f"## {title}", "", f"![{alt}]({artifact('comparison', '.png')})", "",
                  f"[{ae_label}]({artifact('ae', '.png')}) · [{pdf_label}]({artifact('comparison', '.pdf')})", ""]
        if item["status"] == "unavailable":
            lines += [("本项没有可用的本次测量图；请查看下方运行状态。" if zh else
                       "No measured plot is available for this item; see its execution status below."), ""]
        lines += [BOUNDARIES_ZH[key] if zh else item["timing_boundary"], "",
                  "**运行状态**" if zh else "**Execution status**", ""]
        lines += ["- " + line for line in coverage_lines(item["coverage"], language=language)]
        lines.append("")
        if key == "figure-08":
            lines += supplemental_markdown(manifest.get("figure08", {}).get("panels", []), language=language)
    return "\n".join(lines)


class Canvas:
    """Readable pixel panels; the original paper and canonical plots remain unchanged."""
    def __init__(self):
        from PIL import Image, ImageDraw, ImageFont
        import matplotlib
        self.Image, self.ImageDraw = Image, ImageDraw
        fonts = Path(matplotlib.get_data_path()) / "fonts/ttf"
        self.font_directory = fonts
        self.ImageFont = ImageFont
        self.normal = ImageFont.truetype(str(fonts / "DejaVuSans.ttf"), 23)
        self.small = ImageFont.truetype(str(fonts / "DejaVuSans.ttf"), 20)
        self.bold = ImageFont.truetype(str(fonts / "DejaVuSans-Bold.ttf"), 29)

    def text(self, lines, width, *, title=None, color="#263746", background="white"):
        probe = self.ImageDraw.Draw(self.Image.new("RGB", (1, 1)))
        laid_out = []
        if title:
            lines = [(title, self.bold)] + [(str(line), self.small) for line in lines]
        else:
            lines = [(str(line), self.normal) for line in lines]
        for text, font in lines:
            text = timing_text(text)
            # Character wrapping also handles long paths and SHA values.
            current = ""
            for char in text:
                if current and probe.textlength(current + char, font=font) > width - 56:
                    laid_out.append((current, font)); current = ""
                current += char
            laid_out.append((current, font))
        height = 30 + sum(font.size + 12 for _, font in laid_out)
        result = self.Image.new("RGB", (width, height), background)
        draw = self.ImageDraw.Draw(result)
        y = 15
        for line, font in laid_out:
            draw.text((28, y), line, fill=color, font=font)
            y += font.size + 12
        return result

    def picture(self, path, width):
        with self.Image.open(path) as source:
            result = source.convert("RGB")
        result.thumbnail((width, 20000), self.Image.Resampling.LANCZOS)
        panel = self.Image.new("RGB", (width, result.height), "white")
        panel.paste(result, ((width-result.width)//2, 0))
        return panel

    def paper_picture(self, path, width):
        """Equal physical display widths for paper and AE, including upscaling."""
        if isinstance(path, self.Image.Image):
            result = path.convert('RGB')
        else:
            with self.Image.open(path) as source:
                result = source.convert('RGB')
        height = max(1, round(result.height * width / result.width))
        return result.resize((width, height), self.Image.Resampling.LANCZOS)

    def paper_caption(self, picture, caption, bold_prefix=None):
        """Typeset the caption like the paper's surrounding LaTeX."""
        width = picture.width
        size = max(12, round(width * .037))
        font = self.ImageFont.truetype(str(self.font_directory / 'STIXGeneral.ttf'), size)
        bold = self.ImageFont.truetype(str(self.font_directory / 'STIXGeneralBol.ttf'), size)
        probe = self.ImageDraw.Draw(picture)
        label = re.match(r'Figure\s+\d+\.', caption)
        prefix = bold_prefix or (caption.split(', decomposed', 1)[0] if ', decomposed' in caption
                                 else label.group(0) if label else '')
        if prefix and not caption.startswith(prefix):
            raise ValueError('Caption bold prefix does not match its text')
        bold_words = len(prefix.split())
        lines, line, line_width = [], [], 0.
        for index, word in enumerate(caption.split()):
            chosen = bold if index < bold_words or word in ('(a)', '(b)', '(c)') else font
            piece = (' ' if line else '') + word
            extent = probe.textlength(piece, font=chosen)
            if line and line_width + extent > width - 16:
                lines.append(line)
                line, line_width = [], 0.
                piece = word
                extent = probe.textlength(piece, font=chosen)
            line.append((piece, chosen))
            line_width += extent
        if line:
            lines.append(line)
        bottom = max([picture.height] + [
            probe.textbbox((0, picture.height + 8 + index * (size + 4)), text, font=chosen)[3]
            for index, pieces in enumerate(lines) for text, chosen in pieces
        ])
        result = self.Image.new('RGB', (width, bottom + max(6, round(width * .008))), 'white')
        result.paste(picture, (0, 0))
        draw = self.ImageDraw.Draw(result)
        for index, pieces in enumerate(lines):
            x = 4.
            for text, chosen in pieces:
                draw.text((x, picture.height + 8 + index * (size + 4)), text, font=chosen, fill='black')
                x += draw.textlength(text, font=chosen)
        return result

    def paper_pair(self, original, measured, width=1400):
        left, right = (self.paper_picture(path, width) for path in (original, measured))
        heading = 68
        result = self.Image.new('RGB', (2 * width + 36, max(left.height, right.height) + heading), 'white')
        draw = self.ImageDraw.Draw(result)
        draw.text((12, 16), 'Original paper', font=self.bold, fill='#222222')
        draw.text((width + 48, 16), 'AE measurements', font=self.bold, fill='#222222')
        result.paste(left, (0, heading))
        result.paste(right, (width + 36, heading))
        return result

    def vertical(self, panels, width, gap=14):
        result = self.Image.new("RGB", (width, sum(p.height for p in panels) + gap*(len(panels)-1)), "#e8edf0")
        y = 0
        for panel in panels:
            result.paste(panel, ((width-panel.width)//2, y)); y += panel.height + gap
        return result


def build(analysis_path=None, plots_path=None, *, coverage_path, output, paper_dir=AE / "reference/figures", figure08_path=None):
    summary, plots, coverage, paper, references = verify_inputs(analysis_path, plots_path, coverage_path, paper_dir)
    supplement = figure08_supplement(figure08_path, expected_release=source_identity(coverage))
    canvas = Canvas()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    identity = source_identity(coverage)
    version = f"source={identity['source_commit'][:12]}" if identity.get("source_commit") else "source version unavailable"
    if identity.get("source_sha256"):
        version += f"; source SHA-256={identity['source_sha256'][:16]}"
    manifest = dict(schema_version=1, kind="fresh-review-paper-comparison", source="fresh" if summary else "no-fresh-results",
                    release=identity, paper_source_sha256=paper["source_sha256"],
                    coverage=dict(path=str(Path(coverage_path).resolve()), sha256=digest(coverage_path)),
                    analysis=None if not summary else dict(path=str(Path(analysis_path).resolve()), sha256=digest(analysis_path)),
                    plots=None if not plots_path else dict(path=str(Path(plots_path).resolve()), sha256=digest(plots_path)),
                    selection=(summary or {}).get("selection", {}), excluded_runs=(summary or {}).get("excluded_runs", []),
                    note="No archived values are used. Paper-layout columns/bars retain independent population identities; no statistics are pooled across them.", items=[])
    width = 1500
    for key, (title, _, boundary) in ITEMS.items():
        name = "figure-08-cpu" if key == "figure-08" else key
        result = (summary or {}).get("experiments", {}).get(key, {})
        selected = [plot for plot in plots if plot["experiment"] == key]
        rows = matching_coverage(key, coverage)
        panels = [canvas.text([version, boundary, "Fresh selected runs only; full paper cohort is not certified by this image."], width,
                              title=title + " | AE measurements")]
        populations = []
        for number, plot in enumerate(selected, 1):
            selected_groups = plot.get('populations', plot.get('population'))
            records = sample_records(key, result, selected_groups)
            populations.append(dict(population=plot.get("population"),
                                    **({'populations': selected_groups} if isinstance(selected_groups, list) else {}),
                                    plot=plot, samples=records))
            panels += [canvas.text([brief_population(plot.get("population"))], width, title=f"Population {number}"),
                       canvas.picture(plot["resolved_path"], width),
                       canvas.text(sample_lines(key, records), width)]
        limitations = result.get("limitations", [])
        missing = missing_details(result)
        if not selected:
            panels.append(canvas.text(["No successful fresh plot is available for this item."] + limitations[:2], width,
                                      title="NOT MEASURED / NOT ANALYZABLE", color="#984327", background="#fff4ed"))
        elif missing:
            panels.append(canvas.text([json.dumps(missing, ensure_ascii=False)], width,
                                      title="Incomplete panels / comparison arms", color="#984327", background="#fff4ed"))
        panels.append(canvas.text(coverage_lines(rows), width, title="This campaign's execution coverage"))
        ae = canvas.vertical(panels, width)
        paper_panel = canvas.vertical([canvas.text([f"PDF page {references[key]['pdf_page']}; original crop, including caption.",
                                                      "Paper values are reference evidence, not this campaign's measurements."], 1000,
                                                     title=title + " | Original paper"),
                                       canvas.picture(references[key]["path"], 1000)], 1000)
        comparison = canvas.Image.new("RGB", (2540, max(ae.height, paper_panel.height)), "#e8edf0")
        comparison.paste(paper_panel, (0, 0)); comparison.paste(ae, (1040, 0))
        compact_paper = len(selected) == 1 and selected[0].get('layout') == 'paper'
        if compact_paper:
            # README receives the actual paper-format figure, not a dashboard.
            # Hashes, population identities, n, exclusions and coverage stay in
            # manifest.json and the Markdown description alongside the image.
            with canvas.Image.open(selected[0]['resolved_path']) as picture:
                ae = picture.convert('RGB')
            mapping = selected[0].get('data_mapping', {})
            caption = mapping.get('caption')
            if caption:
                ae = canvas.paper_caption(ae, caption, mapping.get('caption_bold_prefix'))
            comparison = canvas.paper_pair(references[key]['path'], ae)
        outputs = []
        for kind, picture in (("ae", ae), ("comparison", comparison)):
            for suffix in ("png", "pdf"):
                path = output / f"{name}-{kind}.{suffix}"
                picture.save(path, **({"resolution": 150.0} if suffix == "pdf" else {}))
                outputs.append(dict(path=path.name, kind=kind, sha256=digest(path), width=picture.width, height=picture.height))
        item = dict(experiment=key, paper_item=title, status="fresh-results" if selected else "unavailable",
                    layout='paper' if compact_paper else 'population-panels',
                    original=references[key], populations=populations, coverage=rows,
                    limitations=limitations, missing=missing, timing_boundary=boundary, artifacts=outputs)
        manifest["items"].append(item)
    if supplement is not None:
        for panel in supplement["panels"]:
            for artifact in panel["artifacts"]:
                source = Path(artifact["path"])
                target = output / source.name
                shutil.copyfile(source, target)
                artifact["source_path"] = str(source.resolve())
                artifact["path"] = target.name
        manifest["figure08"] = supplement
    write_json(output / "manifest.json", manifest)
    for language, filename in (("en", "README.md"), ("zh", "README-zh.md")):
        (output / filename).write_text(markdown_index(manifest, language=language), encoding="utf-8")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", type=Path, help="Canonical source=fresh analysis/summary.json")
    parser.add_argument("--plots", type=Path, help="Canonical plots/plots.json bound to that summary")
    parser.add_argument("--coverage", type=Path, required=True, help="One-click review.json or explicit coverage.json")
    parser.add_argument("--paper-figures", type=Path, default=AE / "reference/figures")
    parser.add_argument("--figure08", type=Path, help="Hash-bound GPU/theory supplement from this run")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = build(args.analysis, args.plots, coverage_path=args.coverage, output=args.output, paper_dir=args.paper_figures, figure08_path=args.figure08)
    except (OSError, ValueError, KeyError, ImportError) as exc:
        parser.exit(2, f"Comparison generation failed: {exc}\n")
    print(f"Generated {len(result['items'])} paper comparison items; {args.output / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
