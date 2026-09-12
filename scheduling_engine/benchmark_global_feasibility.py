"""Generic reduced global-feasibility diagnostics for benchmark DTOs.

The implementation is shared with the preserved v2 diagnostic behavior.  The
public names intentionally do not identify a particular detached lineage, so a
real production-placement qualification can use the same mathematical checks
without importing fixture construction or fixture identity.
"""

from __future__ import annotations

from .paul_desmarais_v2_2_diagnostics import (
    capacity_only_matching as _capacity_only_matching,
    collision_diagnostic as _collision_diagnostic,
)


def capacity_only_matching(data):
    """Check ordinary completion groups against accepted section capacity."""

    return _capacity_only_matching(data)


def reduced_collision_diagnostic(data, *, time_limit_seconds=120.0):
    """Check capacity plus accepted student no-double-booking collisions."""

    result = _collision_diagnostic(data, time_limit_seconds=time_limit_seconds)
    # The preserved diagnostic reports the CP-SAT status directly.  Expose a
    # small boolean alongside it so qualification gates can distinguish a
    # proven feasible/optimal diagnostic from INFEASIBLE or UNKNOWN without
    # changing the underlying model or its lineage semantics.
    result["feasible"] = result.get("status") in {"optimal", "feasible"}
    return result
