"""Compatibility exports for canonical domain values.

New code should import constants from the owning domain module:

- ``backend.apps.common.school_values`` for shared school values;
- ``backend.apps.people.constants`` for role values;
- ``backend.apps.courses.constants`` for catalog/request/offering values;
- ``backend.apps.constraints.constants`` for qualification values;
- ``backend.apps.scheduling.constants`` for planning and lifecycle values.

This file intentionally re-exports those names so older imports continue to
work while the project moves away from one giant miscellaneous constants file.
"""

from backend.apps.common.school_values import *  # noqa: F401,F403
from backend.apps.constraints.constants import *  # noqa: F401,F403
from backend.apps.courses.constants import *  # noqa: F401,F403
from backend.apps.people.constants import *  # noqa: F401,F403
from backend.apps.scheduling.constants import *  # noqa: F401,F403
