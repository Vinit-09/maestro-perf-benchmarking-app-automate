"""Maestro benchmark aggregation + email pipeline.

Pure-Python module owning aggregation, gating, and templating logic. Live BQ
I/O and Gmail send are orchestrated by the Claude session via the project's
already-authenticated MCP connectors; this module produces the SQL strings
and email payloads the orchestrator consumes.

See docs/plans/2026-05-04-001-feat-maestro-benchmark-pipeline-plan.md.
"""

__version__ = "0.1.0"
