
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Position, PositionPermission, User


class PositionPermissionInline(admin.TabularInline):
    model = PositionPermission
    extra = 1

    fields = (
        "module",
        "can_view",
        "can_create",
        "can_update",
        "can_delete",
    )


@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "business",
        "is_active",
        "created_at",
    )

    list_filter = (
        "business",
        "is_active",
    )

    search_fields = (
        "name",
        "business__name",
    )

    inlines = [
        PositionPermissionInline,
    ]


@admin.register(User)
class VGSyncUserAdmin(UserAdmin):

    list_display = (
        "username",
        "email",
        "role",
        "company",
        "business",
        "position",
        "is_active",
        "is_staff",
    )

    list_filter = (
        "role",
        "is_active",
        "is_staff",
        "company",
        "business",
        "position",
    )

    search_fields = (
        "username",
        "email",
        "first_name",
        "last_name",
        "phone",
    )

    fieldsets = UserAdmin.fieldsets + (
        (
            "VGSync Access",
            {
                "fields": (
                    "role",
                    "company",
                    "business",
                    "position",
                    "phone",
                    "must_change_password",
                )
            },
        ),
    )

    add_fieldsets = UserAdmin.add_fieldsets + (
        (
            "VGSync Access",
            {
                "fields": (
                    "email",
                    "role",
                    "company",
                    "business",
                    "position",
                    "phone",
                    "must_change_password",
                )
            },
        ),
    )

    ordering = ("username",)

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)

        # Accounts created here are handed to someone else with a password
        # the admin chose, so by default the new user must set their own
        # password at first login. Untick the box to opt out.
        initial.setdefault("must_change_password", True)

        return initial
