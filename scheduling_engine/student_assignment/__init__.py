"""Public entrypoint for the pure student-assignment solver.

Callers continue to import ``solve_student_assignment`` from this package;
the internal modules are deliberately not part of the solver's public API.
"""

from .core import solve_student_assignment

__all__ = ["solve_student_assignment"]
