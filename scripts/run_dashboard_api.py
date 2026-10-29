"""Read-only JSON API for the live broker dashboard. See src/dashboard_api.py.

    .venv/bin/python -m scripts.run_dashboard_api            # serve on 127.0.0.1:8765
    .venv/bin/python -m scripts.run_dashboard_api --once     # print one snapshot and exit (no server)
"""
import json
import sys

from src import dashboard_api

if "--once" in sys.argv:
    print(json.dumps(dashboard_api.build_snapshot(), indent=2))
else:
    dashboard_api.serve()
