#!/usr/bin/env python3
"""Build an opt-in E2B span probe using a Go overlay, preserving the infra checkout."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

COLLECTOR = r'''
// AE records completed spans in RAM. JSON serialization happens after API timers.
type aePhase struct {
    Name string `json:"name"`
    Start int64 `json:"start_unix_ns"`
    End int64 `json:"end_unix_ns"`
    Trace string `json:"trace_id"`
    Span string `json:"span_id"`
    Parent string `json:"parent_id"`
}
type aePhaseCollector struct { mu sync.Mutex; spans []aePhase }
var aeCollector = &aePhaseCollector{}
func (*aePhaseCollector) OnStart(context.Context, sdktrace.ReadWriteSpan) {}
func (c *aePhaseCollector) OnEnd(s sdktrace.ReadOnlySpan) {
    c.mu.Lock()
    c.spans = append(c.spans, aePhase{s.Name(), s.StartTime().UnixNano(), s.EndTime().UnixNano(), s.SpanContext().TraceID().String(), s.SpanContext().SpanID().String(), s.Parent().SpanID().String()})
    c.mu.Unlock()
}
func (*aePhaseCollector) Shutdown(context.Context) error { return nil }
func (*aePhaseCollector) ForceFlush(context.Context) error { return nil }
func (c *aePhaseCollector) snapshot() []aePhase {
    c.mu.Lock(); defer c.mu.Unlock()
    return append([]aePhase(nil), c.spans...)
}
func aeEnablePhases() {
    if os.Getenv("DELTABOX_E2B_PHASES") == "1" {
        otel.SetTracerProvider(sdktrace.NewTracerProvider(sdktrace.WithSampler(sdktrace.AlwaysSample()), sdktrace.WithSpanProcessor(aeCollector)))
    }
}
'''


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('E2B source boundary changed; review instrumentation: ' + old[:90])
    return text.replace(old, new, 1)


def instrument(text):
    text = replace_once(text, '\t"time"\n', '\t"time"\n\t"sync"\n\t"go.opentelemetry.io/otel"\n\tsdktrace "go.opentelemetry.io/otel/sdk/trace"\n')
    text = replace_once(text, 'func main() {\n', 'func main() {\n\taeEnablePhases()\n')
    text = replace_once(text, 'type pauseTimings struct {\n', 'type pauseTimings struct {\n\taeWindows map[string][2]int64\n')
    start = text.index('func (r *runner) pauseOnce(')
    end = text.index('\nfunc ', start + 1)
    prefix, body, suffix = text[:start], text[start:end], text[end:]
    body = replace_once(body, '\tresumeDur := time.Since(t0)', '\tresumeEnd := time.Now()\n\tresumeDur := resumeEnd.Sub(t0)\n\taeWindows := map[string][2]int64{"resume": {t0.UnixNano(), resumeEnd.UnixNano()}}')
    body = replace_once(body, '\tpauseDur := time.Since(pauseStart)', '\tpauseEnd := time.Now()\n\tpauseDur := pauseEnd.Sub(pauseStart)\n\taeWindows["pause"] = [2]int64{pauseStart.UnixNano(), pauseEnd.UnixNano()}')
    body = replace_once(body, '\ttimings := pauseTimings{\n', '\ttimings := pauseTimings{\n\t\taeWindows: aeWindows,\n')
    body = replace_once(body, '\t\ttimings.upload = time.Since(uploadStart)\n\t\ttimings.total = time.Since(t0)', '\t\tuploadEnd := time.Now()\n\t\ttimings.upload = uploadEnd.Sub(uploadStart)\n\t\ttimings.aeWindows["upload"] = [2]int64{uploadStart.UnixNano(), uploadEnd.UnixNano()}\n\t\ttimings.total = time.Since(t0)')
    text = prefix + body + suffix
    start = text.index('func writeFinalbenchJSON(')
    prefix, body = text[:start], text[start:]
    body = replace_once(body, '\tbody, err := json.MarshalIndent(payload, "", "  ")', '\tif os.Getenv("DELTABOX_E2B_PHASES") == "1" {\n\t\tpayload["phase_windows"] = timings.aeWindows\n\t\tpayload["phase_spans"] = aeCollector.snapshot()\n\t\tpayload["phase_protocol"] = "completed OpenTelemetry spans; monotonic API windows; serialization after timers"\n\t}\n\tbody, err := json.MarshalIndent(payload, "", "  ")')
    return prefix + body + COLLECTOR


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--infra', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--go', type=Path, required=True)
    args = p.parse_args()
    source = args.infra.resolve()/'packages/orchestrator/cmd/resume-build/main.go'
    original = source.read_bytes()
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False)
    patched = output/'main.go'; patched.write_text(instrument(original.decode()))
    subprocess.run([str(args.go.parent/'gofmt'), '-w', str(patched)], check=True)
    overlay = output/'overlay.json'
    overlay.write_text(json.dumps({'Replace': {str(source): str(patched)}}, indent=2)+'\n')
    env = dict(os.environ, GOMAXPROCS="2", GOCACHE=str(args.infra/'.cache-go-build'), GOMODCACHE=str(args.infra/'.cache-go/pkg/mod'))
    command = [str(args.go), 'build', '-p', '2', '-overlay', str(overlay), '-o', str(output/'resume-build'), './cmd/resume-build']
    with (output/'build.log').open('w') as log:
        subprocess.run(command, cwd=args.infra/'packages/orchestrator', env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    if source.read_bytes() != original:
        raise ValueError('Infra source changed during probe build')
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    record = dict(original_source=str(source), original_sha256=hashlib.sha256(original).hexdigest(),
                  overlay_sha256=sha(patched), binary_sha256=sha(output/'resume-build'), command=command,
                  infra_unchanged=True, enable='DELTABOX_E2B_PHASES=1')
    (output/'build.json').write_text(json.dumps(record, indent=2)+'\n')
    print(json.dumps(record))


if __name__ == '__main__':
    main()
