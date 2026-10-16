from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import send_mail


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
    if not settings.EMAIL_HOST_USER or not settings.EMAIL_HOST_PASSWORD:
        raise ImproperlyConfigured("Set SMTP_USER and SMTP_PASSWORD in admin-vca/.env.")
    if not settings.DEFAULT_FROM_EMAIL:
        raise ImproperlyConfigured("Set SMTP_FROM or SMTP_USER in admin-vca/.env.")

    weekdays = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    subject_lines = []
    for code, title, join_code, day_of_week, start_time, end_time, schedule in subjects:
        day_name = weekdays[day_of_week - 1] if 1 <= day_of_week <= len(weekdays) else "Unscheduled"
        subject_lines.append(
            f"- {code} - {title} (join code: {join_code}): "
            f"{day_name}, {start_time}-{end_time} ({schedule.title()})"
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

    send_mail(
        subject="Your MyClaSSes admission is finalized",
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=False,
    )
