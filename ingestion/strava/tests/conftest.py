"""
Makes `import fit_parser` work regardless of the directory pytest is
invoked from. fit_parser.py is a standalone script (ingestion/strava/), not
an installed package, so it isn't importable via the normal package path --
this adds its containing directory to sys.path once, for the whole test
session.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
