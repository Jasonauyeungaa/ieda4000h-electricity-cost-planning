"""ai_compute CLI package.

Entry point: `python -m ai_compute.cli.run` (see run.py for usage).
Runs the ASAP baseline, the cost-minimizing LP optimizer and the INDEPENDENT
checker on a workload packet, and writes structured JSON + human-readable
summary. SYNTHETIC inputs stay labeled; no real-savings claims are made.
"""