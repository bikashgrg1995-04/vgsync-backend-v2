from django.contrib.auth.backends import ModelBackend

from .models import User


class SuperAdminBackend(ModelBackend):

    def authenticate(
        self,
        request,
        username=None,
        password=None,
        **kwargs,
    ):
        if username is None:
            username = kwargs.get(User.USERNAME_FIELD)

        if not username or not password:
            return None

        user = User.objects.filter(
            username=username,
            role=User.Role.SUPER_ADMIN,
        ).first()

        if user and user.check_password(password):
            if self.user_can_authenticate(user):
                return user

        return None