"""Project-wide API authorization and policy-filtering contracts.

These tests are intentionally structural. They do not replace endpoint behavior
tests; they make sure new endpoints declare an explicit access policy and keep
the policy queryset as the first data-access boundary.
"""

from types import SimpleNamespace

import pytest
from django.contrib.auth.models import AnonymousUser
from django.urls import URLPattern, URLResolver, get_resolver

from backend.apps.access.viewsets import PolicyFilteredModelViewSet
from backend.apps.courses.models import Course


def _iter_url_patterns(patterns, prefix=""):
    """Yield concrete URLPattern objects with a readable route prefix."""

    for pattern in patterns:
        route = f"{prefix}{pattern.pattern}"
        if isinstance(pattern, URLResolver):
            yield from _iter_url_patterns(pattern.url_patterns, route)
        elif isinstance(pattern, URLPattern):
            yield route, pattern


def _backend_api_view_classes():
    """Return backend-owned API view classes mounted in the URL tree."""

    for route, pattern in _iter_url_patterns(get_resolver().url_patterns):
        callback = pattern.callback
        view_class = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
        if view_class is None:
            continue
        if not view_class.__module__.startswith("backend.apps."):
            # Admin, SimpleJWT, DRF router root, and similar framework endpoints
            # are allowed to own their own permission contracts.
            continue
        yield route, view_class


def test_backend_api_views_declare_explicit_authorization_policy():
    """Every app endpoint must answer who may access it."""

    missing = []
    for route, view_class in _backend_api_view_classes():
        if view_class.__name__ == "MeView":
            # /api/me/ is the one documented self endpoint; it derives identity
            # from authentication rather than a resource/action policy.
            continue
        has_resource_policy = bool(getattr(view_class, "resource_policy_class", None))
        has_action_policy = bool(getattr(view_class, "action_policy_class", None))
        if not has_resource_policy and not has_action_policy:
            missing.append(f"{route} -> {view_class.__module__}.{view_class.__name__}")

    assert missing == []


@pytest.mark.django_db
def test_policy_filtered_viewset_applies_policy_before_query_filters():
    """A client filter can only narrow objects already allowed by policy."""

    Course.objects.create(
        name="Visible only if policy allows it",
        grade_level=12,
        course_code="TEST4U",
        category="math",
        capacity_min=10,
        capacity_max=30,
    )

    class DenyAllPolicy:
        @staticmethod
        def filter_read_queryset(_user, queryset):
            return queryset.none()

    class ExampleViewSet(PolicyFilteredModelViewSet):
        queryset = Course.objects.all()
        resource_policy_class = DenyAllPolicy
        filter_fields = ("grade_level",)

    view = ExampleViewSet()
    view.request = SimpleNamespace(
        user=AnonymousUser(),
        query_params={"grade_level": "12"},
    )

    assert list(view.get_queryset()) == []


@pytest.mark.django_db
def test_policy_filtered_viewset_without_policy_fails_closed():
    """A missing resource policy must not accidentally expose the base queryset."""

    Course.objects.create(
        name="Should remain hidden",
        grade_level=12,
        course_code="HIDE4U",
        category="math",
        capacity_min=10,
        capacity_max=30,
    )

    class MissingPolicyViewSet(PolicyFilteredModelViewSet):
        queryset = Course.objects.all()

    view = MissingPolicyViewSet()
    view.request = SimpleNamespace(user=AnonymousUser(), query_params={})

    assert list(view.get_queryset()) == []
