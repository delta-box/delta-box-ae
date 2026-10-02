#!/usr/bin/env python3
"""Trusted CPU service entry; never accepts caller environment or arbitrary code.

The launcher has checked the complete source tree and holds its maintenance
lease. This service repeats admission, takes its own lease, verifies the cgroup
swap limit, and execs the normal runner. KillMode=mixed/SIGINT gives the runner
700 seconds to clean up before systemd kills remaining direct descendants.
External Cube/E2B services remain under their existing ownership checks.
Caller SIGKILL cannot run the launcher's signal cleanup; no cancellation
guarantee is made for that case.
"""
import argparse
import importlib.util
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--unit', required=True)
    parser.add_argument('--caller-uid', type=int, required=True)
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
    source = Path(__file__).with_name('hosted_launcher.py')
    spec = importlib.util.spec_from_file_location('hosted_cpu_launcher', source)
    hosted = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hosted)
    return hosted.main(arguments, service_context=(args.caller_uid, args.unit))


if __name__ == '__main__':
    raise SystemExit(main())
