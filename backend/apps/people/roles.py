from backend.apps.people.models import RoleChoices


DOMAIN_PROFILE_ROLES = (
    (RoleChoices.STUDENT, "student_profile"),
    (RoleChoices.TEACHER, "teacher_profile"),
    (RoleChoices.COUNSELOR, "counselor_profile"),
)


def get_user_role(user):
    if not user or not user.is_authenticated:
        return RoleChoices.UNKNOWN

    for role, profile_attr in DOMAIN_PROFILE_ROLES:
        if hasattr(user, profile_attr):
            return role

    if hasattr(user, "role_profile"):
        return user.role_profile.role

    if user.is_superuser:
        return RoleChoices.DIRECTOR

    if user.is_staff:
        return RoleChoices.STAFF

    return RoleChoices.UNKNOWN


def get_user_profile_id(user):
    if not user or not user.is_authenticated:
        return None

    for _, profile_attr in DOMAIN_PROFILE_ROLES:
        if hasattr(user, profile_attr):
            return getattr(user, profile_attr).id

    if hasattr(user, "role_profile"):
        return user.role_profile.id

    return None


def has_role(user, *roles):
    return get_user_role(user) in roles


def has_admin_role(user):
    return bool(
        user
        and user.is_authenticated
        and (
            user.is_superuser
            or has_role(user, RoleChoices.COUNSELOR, RoleChoices.STAFF, RoleChoices.DIRECTOR)
        )
    )
