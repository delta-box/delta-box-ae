#!/usr/bin/env python3
"""Match the native evaluator's single-worker thread for Figure 2 replay."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading


def run_driver(driver, execution_record: Path) -> int:
    """Keep parsing/audit on the main thread and join the one owned worker."""
    original = driver.run_replay
    main_tid = threading.get_native_id()
    invoked = False

    def threaded(*args, **kwargs):
        nonlocal invoked
        if invoked:
            raise RuntimeError('Figure 2 expects one complete recorded input per worker')
        invoked = True

        def invoke():
            record = {'schema_version': 1, 'context': 'single-worker-thread',
                      'pid': os.getpid(), 'main_native_tid': main_tid,
                      'worker_native_tid': threading.get_native_id(),
                      'thread_name': threading.current_thread().name,
                      'cpu_affinity': sorted(os.sched_getaffinity(0)), 'status': 'running'}
            execution_record.write_text(json.dumps(record, indent=2) + '\n')
            try:
                result = original(*args, **kwargs)
                record.update(status='complete' if result == 0 else 'failed', returncode=result)
                return result
            except BaseException as error:
                record.update(status='failed', error=f'{type(error).__name__}: {error}')
                raise
            finally:
                execution_record.write_text(json.dumps(record, indent=2) + '\n')

        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(invoke).result()

    driver.run_replay = threaded
    try:
        return driver.main()
    finally:
        driver.run_replay = original


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--driver', type=Path, required=True)
    parser.add_argument('--execution-record', type=Path, required=True)
    args, forwarded = parser.parse_known_args()
    driver_path = args.driver.resolve(strict=True)
    sys.path.insert(0, str(driver_path.parent))
    spec = importlib.util.spec_from_file_location('figure2_recorded_replay_driver', driver_path)
    driver = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = driver
    spec.loader.exec_module(driver)
    sys.argv = [str(driver_path), *forwarded]
    return run_driver(driver, args.execution_record)


if __name__ == '__main__':
    raise SystemExit(main())
