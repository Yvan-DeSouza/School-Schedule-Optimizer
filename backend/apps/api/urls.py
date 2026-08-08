"""Top-level API composition and JWT session endpoints."""

from django.urls import include, path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from backend.apps.api.views import MeView


urlpatterns = [
    # JWT endpoints are provided by SimpleJWT; /me resolves the application's
    # domain role/profile using central role helpers.
    path("auth/login/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("auth/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("me/", MeView.as_view(), name="me"),
    # Feature URL modules all mount under the single /api/ prefix supplied by
    # config.urls.
    path("", include("backend.apps.courses.urls")),
    path("", include("backend.apps.constraints.urls")),
    path("", include("backend.apps.common.urls")),
    path("", include("backend.apps.scheduling.urls")),
]
