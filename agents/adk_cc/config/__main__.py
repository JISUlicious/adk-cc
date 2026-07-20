"""`python -m adk_cc.config <check|print|gen-env>` — see config/schema.py."""

import sys

from .schema import _main

raise SystemExit(_main(sys.argv[1:]))
