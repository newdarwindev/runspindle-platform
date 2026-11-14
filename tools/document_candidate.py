#!/usr/bin/env python3
"""Read-only plan entry point; no local module bytecode is written."""

import importlib
import sys

sys.dont_write_bytecode = True

if __name__ == "__main__":
    importlib.import_module("runner_fleet.document_candidate").main()
