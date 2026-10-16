from functools import wraps
import logging
import secrets
import smtplib
import uuid

from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db import IntegrityError, connection, transaction
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.core.validators import validate_email
from django.views.decorators.http import require_GET, require_POST
import bcrypt

from .models import AdmissionReviewLog
from .notifications import send_admission_confirmation

logger = logging.getLogger(__name__)

PAGE_SIZE = 30
VALID_STATUSES = {"ALL", "SUBMITTED", "FINALIZED", "REJECTED", "DRAFT"}


class TeacherEmailError(Exception):
    pass


def admin_required(view):
    @wraps(view)
    def wrapped(request: HttpRequest, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"/login/?next={request.path}")
        if not request.user.is_active or not request.user.is_staff or not request.user.is_superuser:
            return HttpResponseForbidden("A Django superuser account is required.")
        return view(request, *args, **kwargs)

    return wrapped


def row_as_dict(cursor, row):
    return dict(zip((column[0] for column in cursor.description), row))


@require_GET
def home(request: HttpRequest) -> HttpResponse:
    user = request.user
    can_manage_admissions = (
        user.is_authenticated and user.is_active and user.is_staff and user.is_superuser
    )
    return render(
        request,
        "admissions/home.html",
        {"can_manage_admissions": can_manage_admissions},
    )


@require_GET
@admin_required
def admission_list(request: HttpRequest) -> HttpResponse:
    status = request.GET.get("status", "SUBMITTED").upper()
    if status not in VALID_STATUSES:
        status = "SUBMITTED"

    where_clause = "" if status == "ALL" else 'WHERE a."status" = %s::"AdmissionStatus"'
    parameters = [] if status == "ALL" else [status]

    with connection.cursor() as cursor:
        cursor.execute(f'SELECT COUNT(*) FROM "admissions" a {where_clause}', parameters)
        total = cursor.fetchone()[0]

    paginator = Paginator(range(total), PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))
    offset = (page.number - 1) * PAGE_SIZE
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT a."id", a."status"::text, a."paymentStatus"::text,
                   a."totalFees", a."submittedAt", a."createdAt",
                   u."username", u."email", s."code" AS "sectionCode",
                   s."course", s."yearLevel"
            FROM "admissions" a
            JOIN "users" u ON u."id" = a."studentId"
            JOIN "course_sections" s ON s."id" = a."sectionId"
            {where_clause}
            ORDER BY COALESCE(a."submittedAt", a."createdAt") DESC
            LIMIT %s OFFSET %s
            """,
            [*parameters, PAGE_SIZE, offset],
        )
        admissions = [row_as_dict(cursor, row) for row in cursor.fetchall()]

    return render(
        request,
        "admissions/admission_list.html",
        {"admissions": admissions, "status": status, "statuses": VALID_STATUSES, "page": page},
    )


@require_GET
@admin_required
def student_list(request: HttpRequest) -> HttpResponse:
    search = request.GET.get("q", "").strip()
    search_filter = 'AND (u."username" ILIKE %s OR u."email" ILIKE %s)' if search else ""
    search_parameters = [f"%{search}%", f"%{search}%"] if search else []

    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT COUNT(*)
            FROM "users" u
            WHERE u."role"::text = 'STUDENT' {search_filter}
            """,
            search_parameters,
        )
        total = cursor.fetchone()[0]

    paginator = Paginator(range(total), PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))
    offset = (page.number - 1) * PAGE_SIZE
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT u."id", u."username", u."email", u."createdAt",
                   course."course", course."yearLevel", course."sectionCode"
            FROM "users" u
            LEFT JOIN LATERAL (
                SELECT s."course", s."yearLevel", s."code" AS "sectionCode"
                FROM "admissions" a
                JOIN "course_sections" s ON s."id" = a."sectionId"
                WHERE a."studentId" = u."id"
                  AND a."status" IN ('SUBMITTED', 'FINALIZED')
                ORDER BY
                  CASE a."status"::text WHEN 'FINALIZED' THEN 0 ELSE 1 END,
                  COALESCE(a."finalizedAt", a."submittedAt", a."createdAt") DESC
                LIMIT 1
            ) course ON TRUE
            WHERE u."role"::text = 'STUDENT' {search_filter}
            ORDER BY u."username"
            LIMIT %s OFFSET %s
            """,
            [*search_parameters, PAGE_SIZE, offset],
        )
        students = [row_as_dict(cursor, row) for row in cursor.fetchall()]

    return render(
        request,
        "admissions/student_list.html",
        {"students": students, "page": page, "search": search},
    )


@require_GET
@admin_required
def teacher_list(request: HttpRequest) -> HttpResponse:
    search = request.GET.get("q", "").strip()
    search_filter = 'AND (u."username" ILIKE %s OR u."email" ILIKE %s)' if search else ""
    search_parameters = [f"%{search}%", f"%{search}%"] if search else []

    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT COUNT(*)
            FROM "users" u
            WHERE u."role"::text = 'TEACHER' {search_filter}
            """,
            search_parameters,
        )
        total = cursor.fetchone()[0]

    paginator = Paginator(range(total), PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))
    offset = (page.number - 1) * PAGE_SIZE
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT u."id", u."username", u."email", u."createdAt",
                   COALESCE(
                     ARRAY_AGG(
                       DISTINCT (sub."code" || ' - ' || sub."title")
                       ORDER BY (sub."code" || ' - ' || sub."title")
                     ) FILTER (WHERE sub."id" IS NOT NULL),
                     ARRAY[]::text[]
                   ) AS "subjects"
            FROM "users" u
            LEFT JOIN "course_sections" s ON s."teacherId" = u."id"
            LEFT JOIN "subjects" sub ON sub."sectionId" = s."id"
            WHERE u."role"::text = 'TEACHER' {search_filter}
            GROUP BY u."id", u."username", u."email", u."createdAt"
            ORDER BY u."username"
            LIMIT %s OFFSET %s
            """,
            [*search_parameters, PAGE_SIZE, offset],
        )
        teachers = [row_as_dict(cursor, row) for row in cursor.fetchall()]

    return render(
        request,
        "admissions/teacher_list.html",
        {"teachers": teachers, "page": page, "search": search},
    )


@require_GET
@admin_required
def create_teacher(request: HttpRequest) -> HttpResponse:
    return render(request, "admissions/teacher_create.html")


@require_POST
@admin_required
def create_teacher_submit(request: HttpRequest) -> HttpResponse:
    username = request.POST.get("username", "").strip()
    email = request.POST.get("email", "").strip().lower()
    if len(username) < 3 or len(username) > 30:
        messages.error(request, "Username must be between 3 and 30 characters.")
        return redirect("admissions:create_teacher")
    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, "Enter a valid email address.")
        return redirect("admissions:create_teacher")
    if len(email) > 254:
        messages.error(request, "Email address must be 254 characters or fewer.")
        return redirect("admissions:create_teacher")

    temporary_password = secrets.token_urlsafe(24)
    password_hash = bcrypt.hashpw(
        temporary_password.encode("utf-8"),
        bcrypt.gensalt(rounds=12),
    ).decode("utf-8")
    teacher_id = str(uuid.uuid4())

    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT 1 FROM "users"
                    WHERE "username" = %s OR LOWER("email") = %s
                    LIMIT 1
                    """,
                    [username, email],
                )
                if cursor.fetchone():
                    messages.error(request, "That username or email is already registered.")
                    return redirect("admissions:create_teacher")
                cursor.execute(
                    """
                    INSERT INTO "users"
                      ("id", "username", "email", "passwordHash", "role",
                       "emailVerified", "mustChangePassword", "createdAt", "updatedAt")
                    VALUES (%s, %s, %s, %s, 'TEACHER'::"UserRole", TRUE, TRUE, NOW(), NOW())
                    """,
                    [teacher_id, username, email, password_hash],
                )
            try:
                send_mail(
                    subject="Your MyClaSSes teacher account",
                    message=(
                        f"Hello {username},\n\n"
                        f"Your teacher account is ready. Sign in to the MyClaSSes mobile app "
                        f"with your email and this temporary password:\n\n{temporary_password}\n\n"
                        "You will be asked to change it after signing in."
                    ),
                    from_email=None,
                    recipient_list=[email],
                    fail_silently=False,
                )
            except (OSError, smtplib.SMTPException, ImproperlyConfigured):
                logger.exception("Could not send teacher account email to %s", email)
                raise TeacherEmailError from None
    except TeacherEmailError:
        messages.error(
            request,
            "The teacher account was not created because its temporary password could not be emailed. "
            "Check Gmail SMTP settings and try again.",
        )
        return redirect("admissions:create_teacher")
    except IntegrityError:
        logger.exception("Could not create teacher account for %s because of a database constraint", email)
        messages.error(request, "That username or email is already registered.")
        return redirect("admissions:create_teacher")

    messages.success(request, f"Teacher account created. A temporary password was emailed to {email}.")
    return redirect("admissions:teachers")


@require_GET
@admin_required
def assign_teachers(request: HttpRequest) -> HttpResponse:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT s."id", s."code", s."course", s."yearLevel", s."semester",
                   s."teacherId", u."username" AS "teacherUsername",
                   COALESCE(
                     ARRAY_AGG(DISTINCT (sub."code" || ' - ' || sub."title")
                       ORDER BY (sub."code" || ' - ' || sub."title"))
                       FILTER (WHERE sub."id" IS NOT NULL),
                     ARRAY[]::text[]
                   ) AS "subjects"
            FROM "course_sections" s
            LEFT JOIN "users" u ON u."id" = s."teacherId"
            LEFT JOIN "subjects" sub ON sub."sectionId" = s."id"
            GROUP BY s."id", u."username"
            ORDER BY s."course", s."yearLevel", s."code"
            """
        )
        sections = [row_as_dict(cursor, row) for row in cursor.fetchall()]
        cursor.execute(
            """
            SELECT "id", "username", "email"
            FROM "users"
            WHERE "role"::text = 'TEACHER'
            ORDER BY "username"
            """
        )
        teachers = [row_as_dict(cursor, row) for row in cursor.fetchall()]

    return render(
        request,
        "admissions/section_assignments.html",
        {"sections": sections, "teachers": teachers},
    )


@require_POST
@admin_required
def assign_teacher_submit(request: HttpRequest) -> HttpResponse:
    section_id = request.POST.get("section_id", "").strip()
    teacher_id = request.POST.get("teacher_id", "").strip() or None
    try:
        uuid.UUID(section_id)
        if teacher_id:
            uuid.UUID(teacher_id)
    except ValueError:
        messages.error(request, "Choose a valid section and teacher.")
        return redirect("admissions:assign_teachers")

    with connection.cursor() as cursor:
        if teacher_id:
            cursor.execute(
                'SELECT 1 FROM "users" WHERE "id" = %s AND "role"::text = \'TEACHER\'',
                [teacher_id],
            )
            if cursor.fetchone() is None:
                messages.error(request, "Choose a valid teacher account.")
                return redirect("admissions:assign_teachers")
        cursor.execute(
            'UPDATE "course_sections" SET "teacherId" = %s WHERE "id" = %s',
            [teacher_id, section_id],
        )
        if cursor.rowcount != 1:
            messages.error(request, "Course section not found.")
            return redirect("admissions:assign_teachers")

    messages.success(request, "Teacher assignment updated.")
    return redirect("admissions:assign_teachers")


@require_GET
@admin_required
def admission_detail(request: HttpRequest, admission_id: str) -> HttpResponse:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT a."id", a."status"::text, a."paymentStatus"::text,
                   a."enrollmentFee", a."subjectFees", a."otherFees", a."totalFees",
                   a."submittedAt", a."finalizedAt", a."createdAt",
                   u."username", u."email", u."mobilePhone", u."age", u."sex"::text,
                   u."blk", u."lot", u."street", u."villagePurok", u."barangay",
                   u."municipality", u."region",
                   s."id" AS "sectionId", s."code" AS "sectionCode",
                   s."course", s."yearLevel", s."semester"
            FROM "admissions" a
            JOIN "users" u ON u."id" = a."studentId"
            JOIN "course_sections" s ON s."id" = a."sectionId"
            WHERE a."id" = %s
            """,
            [admission_id],
        )
        row = cursor.fetchone()
        if row is None:
            raise Http404("Admission not found.")
        admission = row_as_dict(cursor, row)

        cursor.execute(
            """
            SELECT sub."code", sub."title", aus."joinCode" AS "joinCode", sub."units", sub."dayOfWeek",
                   sub."startTime", sub."endTime", aus."schedule"
            FROM "admission_subjects" aus
            JOIN "subjects" sub ON sub."id" = aus."subjectId"
            WHERE aus."admissionId" = %s
            ORDER BY sub."code"
            """,
            [admission_id],
        )
        subjects = [row_as_dict(cursor, subject) for subject in cursor.fetchall()]

        cursor.execute(
            """
            SELECT "id", "code"
            FROM "course_sections"
            WHERE "course" = %s AND "yearLevel" = %s AND "semester" = %s
            ORDER BY "code"
            """,
            [admission["course"], admission["yearLevel"], admission["semester"]],
        )
        sections = [row_as_dict(cursor, section) for section in cursor.fetchall()]

    return render(
        request,
        "admissions/admission_detail.html",
        {"admission": admission, "subjects": subjects, "sections": sections},
    )


@require_POST
@admin_required
def review_admission(request: HttpRequest, admission_id: str) -> HttpResponse:
    action = request.POST.get("action")
    confirmed = request.POST.get("confirm") == "yes"
    reason = request.POST.get("reason", "").strip()

    if action not in {"accept", "reject"}:
        messages.error(request, "Choose a valid review action.")
        return redirect("admissions:detail", admission_id=admission_id)
    if not confirmed:
        messages.error(request, "Check the confirmation box before submitting this decision.")
        return redirect("admissions:detail", admission_id=admission_id)
    if action == "reject" and not reason:
        messages.error(request, "Enter a reason for rejecting this admission.")
        return redirect("admissions:detail", admission_id=admission_id)
    if len(reason) > 2000:
        messages.error(request, "The rejection reason must be 2,000 characters or fewer.")
        return redirect("admissions:detail", admission_id=admission_id)

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT a."status"::text, a."studentId", a."sectionId",
                       s."course", s."yearLevel", s."semester",
                       u."username", u."email", s."code"
                FROM "admissions" a
                JOIN "course_sections" s ON s."id" = a."sectionId"
                JOIN "users" u ON u."id" = a."studentId"
                WHERE a."id" = %s
                FOR UPDATE OF a
                """,
                [admission_id],
            )
            row = cursor.fetchone()
            if row is None:
                raise Http404("Admission not found.")
            (
                status,
                student_id,
                current_section_id,
                course,
                year_level,
                semester,
                username,
                email,
                section_code,
            ) = row
            if status != "SUBMITTED":
                messages.error(request, "Only submitted admissions can be accepted or rejected.")
                return redirect("admissions:detail", admission_id=admission_id)

            if action == "accept":
                section_id = request.POST.get("section_id", current_section_id).strip()
                if not section_id:
                    messages.error(request, "Choose a section for this student.")
                    return redirect("admissions:detail", admission_id=admission_id)

                subject_rows = []
                if section_id != current_section_id:
                    cursor.execute(
                        """
                        SELECT "id", "code"
                        FROM "course_sections"
                        WHERE "id" = %s AND "course" = %s
                          AND "yearLevel" = %s AND "semester" = %s
                        """,
                        [section_id, course, year_level, semester],
                    )
                    target_section = cursor.fetchone()
                    if target_section is None:
                        messages.error(
                            request,
                            "Choose a section for the same course, year, and semester.",
                        )
                        return redirect("admissions:detail", admission_id=admission_id)
                    section_code = target_section[1]

                    cursor.execute(
                        """
                        SELECT 1 FROM "admissions"
                        WHERE "studentId" = %s AND "sectionId" = %s AND "id" <> %s
                        LIMIT 1
                        """,
                        [student_id, section_id, admission_id],
                    )
                    if cursor.fetchone() is not None:
                        messages.error(
                            request,
                            "This student already has an admission for the selected section.",
                        )
                        return redirect("admissions:detail", admission_id=admission_id)

                    cursor.execute(
                        """
                        SELECT aus."subjectId", sub."code"
                        FROM "admission_subjects" aus
                        JOIN "subjects" sub ON sub."id" = aus."subjectId"
                        WHERE aus."admissionId" = %s
                        """,
                        [admission_id],
                    )
                    subject_rows = cursor.fetchall()
                    cursor.execute(
                        'SELECT "id", "code" FROM "subjects" WHERE "sectionId" = %s',
                        [section_id],
                    )
                    target_subject_ids = {code: subject_id for subject_id, code in cursor.fetchall()}
                    if any(code not in target_subject_ids for _, code in subject_rows):
                        messages.error(
                            request,
                            "The selected section does not include all subjects in this application.",
                        )
                        return redirect("admissions:detail", admission_id=admission_id)

                cursor.execute(
                    """
                    UPDATE "admissions"
                    SET "sectionId" = %s, "status" = 'FINALIZED', "paymentStatus" = 'PAID',
                        "finalizedAt" = NOW(), "updatedAt" = NOW()
                    WHERE "id" = %s AND "status" = 'SUBMITTED'
                    """,
                    [section_id, admission_id],
                )
                if cursor.rowcount != 1:
                    messages.error(request, "This admission changed while you were reviewing it. Reload and try again.")
                    return redirect("admissions:detail", admission_id=admission_id)
                for subject_id, code in subject_rows:
                    cursor.execute(
                        """
                        UPDATE "admission_subjects" SET "subjectId" = %s
                        WHERE "admissionId" = %s AND "subjectId" = %s
                        """,
                        [target_subject_ids[code], admission_id, subject_id],
                    )

                cursor.execute(
                    """
                    SELECT sub."code", sub."title", aus."joinCode", sub."dayOfWeek",
                           sub."startTime", sub."endTime", aus."schedule"
                    FROM "admission_subjects" aus
                    JOIN "subjects" sub ON sub."id" = aus."subjectId"
                    WHERE aus."admissionId" = %s
                    ORDER BY sub."code"
                    """,
                    [admission_id],
                )
                subjects = cursor.fetchall()
                try:
                    send_admission_confirmation(
                        email=email,
                        username=username,
                        course=course,
                        section_code=section_code,
                        year_level=year_level,
                        semester=semester,
                        subjects=subjects,
                    )
                except (OSError, smtplib.SMTPException, ImproperlyConfigured):
                    logger.exception("Could not email admission confirmation for %s", admission_id)
                    transaction.set_rollback(True)
                    messages.error(
                        request,
                        "The admission was not finalized because its confirmation email could not be sent. "
                        "Check the Gmail SMTP settings and try again.",
                    )
                    return redirect("admissions:detail", admission_id=admission_id)

                log_action = AdmissionReviewLog.Action.ACCEPTED
                log_reason = ""
                success_message = "Admission finalized and confirmation email sent to the student."
            else:
                cursor.execute(
                    """
                    UPDATE "admissions"
                    SET "status" = 'REJECTED', "updatedAt" = NOW()
                    WHERE "id" = %s AND "status" = 'SUBMITTED'
                    """,
                    [admission_id],
                )
                if cursor.rowcount != 1:
                    messages.error(request, "This admission changed while you were reviewing it. Reload and try again.")
                    return redirect("admissions:detail", admission_id=admission_id)
                log_action = AdmissionReviewLog.Action.REJECTED
                log_reason = reason
                success_message = "Admission rejected."

        AdmissionReviewLog.objects.create(
            admission_id=admission_id,
            admin=request.user,
            action=log_action,
            reason=log_reason,
        )

    messages.success(request, success_message)
    return redirect("admissions:detail", admission_id=admission_id)
