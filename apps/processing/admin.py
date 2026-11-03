from django.contrib import admin

from .models import EmbeddingCenter


@admin.register(EmbeddingCenter)
class EmbeddingCenterAdmin(admin.ModelAdmin):
    """Jen pro čtení: střed se počítá automaticky nebo příkazem process_documents --recenter."""

    list_display = ["model_name", "n_documents", "computed_at"]
    exclude = ["vector"]
    readonly_fields = ["model_name", "n_documents", "computed_at"]

    def has_add_permission(self, request):
        return False
