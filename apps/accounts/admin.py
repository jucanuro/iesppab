from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from apps.accounts.models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    model = User

    list_display = (
        "username",
        "email",
        "first_name",
        "last_name",
        "role",
        "institution",
        "is_active",
        "is_staff",
    )
    list_filter = (
        "role",
        "institution",
        "is_active",
        "is_staff",
        "is_superuser",
    )
    search_fields = (
        "username",
        "email",
        "first_name",
        "last_name",
        "document_number",
    )
    ordering = ("username",)

    fieldsets = DjangoUserAdmin.fieldsets + (
        (
            "Datos institucionales",
            {
                "fields": (
                    "institution",
                    "role",
                    "document_number",
                    "phone",
                    "must_change_password",
                )
            },
        ),
    )

    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        (
            "Datos institucionales",
            {
                "fields": (
                    "email",
                    "institution",
                    "role",
                    "document_number",
                    "phone",
                )
            },
        ),
    )

    readonly_fields = ("last_login", "date_joined")