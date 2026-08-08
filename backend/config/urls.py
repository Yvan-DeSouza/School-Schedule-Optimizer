"""Project URL root: Django admin plus the version-one API surface."""
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    # Feature modules compose beneath /api/; one prefix simplifies auth clients
    # and leaves a clean place for future API versioning.
    path('admin/', admin.site.urls),
    path('api/', include('backend.apps.api.urls')),
]
