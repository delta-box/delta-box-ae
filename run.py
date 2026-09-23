#!/usr/bin/env python3
"""Compatibility entry; the live agent is maintained in agent/."""
from agent.run import main
if __name__ == "__main__":
    raise SystemExit(main())
