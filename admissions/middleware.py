import time

from django.conf import settings
from django.contrib.auth import logout
from django.shortcuts import redirect


class AdminSessionTimeoutMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if (
            user.is_authenticated
            and user.is_active
            and user.is_staff
            and user.is_superuser
        ):
            now = time.time()
            started_at = request.session.get("admin_session_started_at")
            last_activity = request.session.get("admin_session_last_activity")

            if started_at is None and last_activity is None:
                request.session["admin_session_started_at"] = now
                request.session["admin_session_last_activity"] = now
            elif (
                type(started_at) not in (int, float)
                or type(last_activity) not in (int, float)
                or now < started_at
                or now < last_activity
                or now - last_activity >= settings.ADMIN_SESSION_IDLE_TIMEOUT_SECONDS
                or now - started_at >= settings.ADMIN_SESSION_ABSOLUTE_TIMEOUT_SECONDS
            ):
                logout(request)
                return redirect("/login/?session_expired=1")
            else:
                request.session["admin_session_last_activity"] = now

        return self.get_response(request)
