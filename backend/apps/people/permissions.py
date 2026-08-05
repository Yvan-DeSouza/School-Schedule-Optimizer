from rest_framework.permissions import BasePermission

from backend.apps.people.models import RoleChoices
from backend.apps.people.roles import get_user_role, has_admin_role, has_role


class IsDirector(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and (request.user.is_superuser or has_role(request.user, RoleChoices.DIRECTOR))
        )


class IsStaff(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and (
                request.user.is_superuser
                or has_role(request.user, RoleChoices.STAFF, RoleChoices.DIRECTOR)
            )
        )


class IsCounselor(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and (
                request.user.is_superuser
                or has_role(request.user, RoleChoices.COUNSELOR, RoleChoices.DIRECTOR)
            )
        )


class IsTeacher(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and (request.user.is_superuser or has_role(request.user, RoleChoices.TEACHER))
        )


class IsStudent(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and (request.user.is_superuser or has_role(request.user, RoleChoices.STUDENT))
        )


class IsOwnerOrCounselor(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        if request.user.is_superuser or has_admin_role(request.user):
            return True

        owner = getattr(obj, "user", None)
        if owner is not None:
            return owner == request.user

        student = getattr(obj, "student", None)
        if student is not None:
            return getattr(student, "user_id", None) == request.user.id

        teacher = getattr(obj, "teacher", None)
        if teacher is not None:
            return getattr(teacher, "user_id", None) == request.user.id

        counselor = getattr(obj, "counselor", None)
        if counselor is not None:
            return getattr(counselor, "user_id", None) == request.user.id

        return False


class HasKnownRole(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and get_user_role(request.user) != RoleChoices.UNKNOWN
        )
