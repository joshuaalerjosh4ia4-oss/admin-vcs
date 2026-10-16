from django.contrib import admin

from .models import AdmissionReviewLog


@admin.register(AdmissionReviewLog)
class AdmissionReviewLogAdmin(admin.ModelAdmin):
    list_display = ("admission_id", "action", "admin", "created_at")
    list_filter = ("action", "created_at")
    search_fields = ("admission_id", "admin__username", "reason")
    readonly_fields = ("id", "admission_id", "admin", "action", "reason", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
