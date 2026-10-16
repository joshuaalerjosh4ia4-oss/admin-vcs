import uuid

from django.conf import settings
from django.db import models


class AdmissionReviewLog(models.Model):
    class Action(models.TextChoices):
        ACCEPTED = "ACCEPTED", "Accepted"
        REJECTED = "REJECTED", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    admission_id = models.CharField(max_length=36)
    admin = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=8, choices=Action.choices)
    reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "admin_vca_admission_review_log"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.action} admission {self.admission_id} by {self.admin}"
