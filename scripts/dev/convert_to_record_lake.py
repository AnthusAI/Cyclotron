#!/usr/bin/env python3
"""Convert one lab account's embedded history into the record lake. Dry run unless --apply is given.

All behaviour lives in decision_flywheel.record_lake_conversion; run it with --help for the options.
"""

import sys

from decision_flywheel.record_lake_conversion import main

if __name__ == "__main__":
    sys.exit(main())
