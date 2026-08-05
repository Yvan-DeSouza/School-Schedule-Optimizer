from rest_framework.response import Response
from rest_framework.views import APIView

from backend.apps.people.roles import get_user_profile_id, get_user_role


class MeView(APIView):
    def get(self, request):
        return Response(
            {
                "id": request.user.id,
                "username": request.user.username,
                "email": request.user.email,
                "role": get_user_role(request.user),
                "profile_id": get_user_profile_id(request.user),
            }
        )
