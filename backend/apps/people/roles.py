"""Central role resolution helpers shared by policies and permissions."""

from backend.apps.people.models import RoleChoices


# Profile order is deliberate and checked before generic flags/role records.
DOMAIN_PROFILE_ROLES = (
    (RoleChoices.STUDENT, "student_profile"),
    (RoleChoices.TEACHER, "teacher_profile"),
    (RoleChoices.COUNSELOR, "counselor_profile"),
)


def get_user_role(user):
    """Resolve one recognized application role, failing closed to UNKNOWN."""

    if not user or not user.is_authenticated:
        return RoleChoices.UNKNOWN

    for role, profile_attr in DOMAIN_PROFILE_ROLES:
        # A concrete domain profile is the strongest evidence of role.
        if hasattr(user, profile_attr):
            return role

    if hasattr(user, "role_profile"):
        # Explicit staff/director/unknown role record precedes Django flags.
        return user.role_profile.role

    if user.is_superuser:
        # Flag fallbacks support administrative accounts lacking role profiles.
        return RoleChoices.DIRECTOR

    if user.is_staff:
        return RoleChoices.STAFF

    return RoleChoices.UNKNOWN


def get_user_profile_id(user):
    """Return the resolved domain/role profile ID, or None for flag-only roles."""

    if not user or not user.is_authenticated:
        return None

    for _, profile_attr in DOMAIN_PROFILE_ROLES:
        if hasattr(user, profile_attr):
            return getattr(user, profile_attr).id

    if hasattr(user, "role_profile"):
        return user.role_profile.id

    return None


def has_role(user, *roles):
    """Convenience predicate using the same central resolution rules."""

    return get_user_role(user) in roles


def has_admin_role(user):
    """Return whether a user belongs to any planning/administrative role."""

    return bool(
        user
        and user.is_authenticated
        and (
            user.is_superuser
            or has_role(user, RoleChoices.COUNSELOR, RoleChoices.STAFF, RoleChoices.DIRECTOR)
        )
    )
