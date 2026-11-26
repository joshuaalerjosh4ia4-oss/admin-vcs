import os

from django.contrib.auth import get_user_model, password_validation
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError


class Command(BaseCommand):
    help = "Create the configured deployment superuser if it does not exist."

    def handle(self, *args, **options):
        user_model = get_user_model()
        username_field = user_model.USERNAME_FIELD
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "").strip()
        if not username:
            raise CommandError("Set DJANGO_SUPERUSER_USERNAME before provisioning.")

        manager = user_model._default_manager
        user = manager.filter(**{username_field: username}).first()
        if user is not None:
            if not user.is_superuser:
                raise CommandError(
                    f"The account {username!r} exists but is not a superuser."
                )
            self.stdout.write(f"Superuser {username!r} already exists; skipping.")
            return

        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "")
        if not password:
            raise CommandError(
                "Set DJANGO_SUPERUSER_PASSWORD to provision a new superuser."
            )

        email_field = user_model.get_email_field_name()
        email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "")
        candidate = user_model(**{username_field: username})
        if email_field != username_field:
            setattr(candidate, email_field, email)
        try:
            password_validation.validate_password(password, candidate)
        except ValidationError as error:
            raise CommandError("; ".join(error.messages)) from error

        credentials = {username_field: username, "password": password}
        if email_field not in credentials:
            credentials[email_field] = email
        manager.create_superuser(**credentials)
        self.stdout.write(self.style.SUCCESS(f"Created superuser {username!r}."))
