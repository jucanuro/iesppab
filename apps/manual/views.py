from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import redirect
from django.views import View
from django.views.generic import TemplateView

from apps.manual.services import ManualGenerationError, ManualService


class ManualView(LoginRequiredMixin, TemplateView):
    template_name = "manual/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(ManualService().build_context(self.request.user))
        return context


class ManualDownloadView(LoginRequiredMixin, View):
    def get(self, request):
        try:
            pdf = ManualService().render_pdf(request.user)
        except ManualGenerationError as exc:
            messages.error(request, str(exc))
            return redirect("manual:index")
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = 'attachment; filename="manual-de-usuario.pdf"'
        return response
