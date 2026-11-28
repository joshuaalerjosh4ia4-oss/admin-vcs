import base64
import json
from email.message import EmailMessage
from email.policy import SMTP
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .time_format import format_time_12_hour


class GmailAPIError(Exception):
    pass


def _gmail_api_request(request: Request) -> dict[str, object]:
    try:
        with urlopen(request, timeout=settings.GMAIL_API_TIMEOUT) as response:
            response_body = response.read()
    except HTTPError as error:
        try:
            error_payload = json.loads(error.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            error_payload = {}
        if not isinstance(error_payload, dict):
            error_payload = {}
        detail = error_payload.get("error")
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("status")
        elif isinstance(detail, str):
            detail = error_payload.get("error_description") or detail
        raise GmailAPIError(
            f"Gmail API returned HTTP {error.code}"
            + (f": {detail}" if detail else ".")
        ) from error
    except URLError as error:
        raise GmailAPIError(f"Could not connect to Gmail API: {error.reason}") from error

    try:
        response_payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GmailAPIError("Gmail API returned an invalid response.") from error
    if not isinstance(response_payload, dict):
        raise GmailAPIError("Gmail API returned an invalid response.")
    return response_payload


def send_gmail_message(*, subject: str, message: str, recipient: str) -> None:
    required_settings = {
        "GMAIL_OAUTH_CLIENT_ID": settings.GMAIL_OAUTH_CLIENT_ID,
        "GMAIL_OAUTH_CLIENT_SECRET": settings.GMAIL_OAUTH_CLIENT_SECRET,
        "GMAIL_OAUTH_REFRESH_TOKEN": settings.GMAIL_OAUTH_REFRESH_TOKEN,
        "GMAIL_FROM_EMAIL": settings.GMAIL_FROM_EMAIL,
    }
    missing_settings = [name for name, value in required_settings.items() if not value]
    if missing_settings:
        raise ImproperlyConfigured(
            f"Set {', '.join(missing_settings)} in the environment."
        )

    token_request = Request(
        "https://oauth2.googleapis.com/token",
        data=urlencode(
            {
                "client_id": settings.GMAIL_OAUTH_CLIENT_ID,
                "client_secret": settings.GMAIL_OAUTH_CLIENT_SECRET,
                "refresh_token": settings.GMAIL_OAUTH_REFRESH_TOKEN,
                "grant_type": "refresh_token",
            }
        ).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    token_response = _gmail_api_request(token_request)
    access_token = token_response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise GmailAPIError("Gmail OAuth response did not contain an access token.")

    email_message = EmailMessage()
    email_message["To"] = recipient
    email_message["From"] = settings.GMAIL_FROM_EMAIL
    email_message["Subject"] = subject
    email_message.set_content(message)
    raw_message = base64.urlsafe_b64encode(
        email_message.as_bytes(policy=SMTP)
    ).decode("ascii").rstrip("=")

    send_request = Request(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        data=json.dumps({"raw": raw_message}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    _gmail_api_request(send_request)


def send_admission_confirmation(
    *,
    email: str,
    username: str,
    course: str,
    section_code: str,
    year_level: int,
    semester: str,
    subjects: list[tuple[str, str, str, int, str, str, str]],
) -> None:
    weekdays = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    subject_lines = []
    for code, title, join_code, day_of_week, start_time, end_time, schedule in subjects:
        day_name = weekdays[day_of_week - 1] if 1 <= day_of_week <= len(weekdays) else "Unscheduled"
        subject_lines.append(
            f"- {code} - {title} (join code: {join_code}): "
            f"{day_name}, {format_time_12_hour(start_time)}-{format_time_12_hour(end_time)} ({schedule.title()})"
        )

    subjects_text = "\n".join(subject_lines) if subject_lines else "- No subjects are currently listed."
    message = (
        f"Hello {username},\n\n"
        "Your admission has been finalized and your payment has been confirmed.\n\n"
        f"Course: {course}\n"
        f"Section: {section_code}\n"
        f"Year: {year_level}\n"
        f"Semester: {semester}\n\n"
        f"Your subject schedule:\n{subjects_text}\n\n"
        "Enter a subject's 6-digit join code in the Classroom tab to add it. "
        "Video meeting IDs and passcodes are shared separately by your instructor.\n\n"
        "Thank you,\nMyClaSSes"
    )

    send_gmail_message(
        subject="Your MyClaSSes admission is finalized",
        message=message,
        recipient=email,
    )
