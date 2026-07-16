#!/usr/bin/env python3
"""Test entrypoint for ingest/parse_health_connect.py (see _helpers.py)."""

from _helpers import run_module_selfcheck

if __name__ == "__main__":
    print(run_module_selfcheck("ingest/parse_health_connect.py"))
