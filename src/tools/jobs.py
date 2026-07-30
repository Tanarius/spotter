"""list_job_applications — give the brain read access to the job pipeline.

The tracker itself is dashboard-managed (add/update happens on the web); this
tool closes the gap where chat had no way to see job_applications at all.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..db.models import JobApplication
from .base import ToolContext

_STATUSES = (
    "applied",
    "responded",
    "screen",
    "interview",
    "offer",
    "rejected",
    "ghosted",
)
_MAX_ROWS = 30


def list_job_applications(ctx: ToolContext, tool_input: dict[str, Any]) -> str:
    """List tracked applications, optionally filtered by status."""
    status = (tool_input.get("status") or "").strip().lower()
    if status and status not in _STATUSES:
        return f"Invalid status '{status}'. Valid: {', '.join(_STATUSES)}."

    query = select(JobApplication).order_by(
        JobApplication.date_applied.desc(), JobApplication.id.desc()
    )
    if status:
        query = query.where(JobApplication.status == status)
    rows = ctx.session.scalars(query.limit(_MAX_ROWS)).all()

    if not rows:
        scope = f" with status '{status}'" if status else ""
        return (
            f"No job applications{scope} are tracked. They're added and updated "
            "on the dashboard's Job applications section."
        )

    counts: dict[str, int] = {}
    for row in ctx.session.scalars(select(JobApplication)):
        counts[row.status] = counts.get(row.status, 0) + 1
    summary = " · ".join(f"{counts[s]} {s}" for s in _STATUSES if counts.get(s))

    lines = [f"Job applications ({summary}):"]
    for a in rows:
        source = f", via {a.source}" if a.source else ""
        notes = f" — {a.notes}" if a.notes else ""
        lines.append(
            f"- #{a.id} {a.company} — {a.role} [{a.status}] "
            f"applied {a.date_applied}{source}{notes}"
        )
    return "\n".join(lines)
