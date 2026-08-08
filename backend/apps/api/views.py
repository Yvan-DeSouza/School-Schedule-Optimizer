"""Authenticated identity endpoint used to bootstrap role-aware clients."""

from rest_framework.response import Response
from rest_framework.views import APIView

from backend.apps.people.roles import get_user_profile_id, get_user_role


class MeView(APIView):
    """Return auth identity plus centrally resolved application role/profile."""

    def get(self, request):
        # Default IsAuthenticated applies from settings, so every authenticated
        # role may safely inspect only its own identity.
        return Response(
            {
                "id": request.user.id,
                "username": request.user.username,
                "email": request.user.email,
                "role": get_user_role(request.user),
                "profile_id": get_user_profile_id(request.user),
            }
        )
