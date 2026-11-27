from __future__ import annotations

from django.contrib.admin.forms import AdminAuthenticationForm


class InstitutionalAdminAuthenticationForm(AdminAuthenticationForm):
    """Login de /admin/: acepta usuario o correo institucional (staff)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["username"].label = "Usuario o correo institucional"
        self.fields["username"].max_length = 254
        self.fields["username"].widget.attrs["maxlength"] = 254
