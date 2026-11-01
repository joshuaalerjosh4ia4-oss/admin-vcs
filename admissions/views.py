from functools import wraps
import logging
import re
import secrets
import smtplib
import uuid
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.db import IntegrityError, connection, transaction
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST
import bcrypt

from .models import AdmissionReviewLog
from .notifications import send_admission_confirmation

logger = logging.getLogger(__name__)

PAGE_SIZE = 30
SECTION_SUBJECT_LINE = re.compile(
    r"^(?P<code>[^|]{1,30})\s*\|\s*(?P<title>[^|]{1,200})\s*\|"
    r"\s*(?P<units>[1-6])\s*\|\s*(?P<day>[1-7])\s*\|"
    r"\s*(?P<start>(?:[01]\d|2[0-3]):[0-5]\d)\s*\|\s*"
    r"(?P<end>(?:[01]\d|2[0-3]):[0-5]\d)$"
)
SUBJECT_TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
WEEKDAY_NAMES = {
    1: "Monday",
    2: "Tuesday",
    3: "Wednesday",
    4: "Thursday",
    5: "Friday",
    6: "Saturday",
    7: "Sunday",
}
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


def parse_section_subjects(value: str):
    subjects = []
    seen_codes = set()
    for line_number, raw_line in enumerate(value.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        match = SECTION_SUBJECT_LINE.fullmatch(line)
        if match is None:
            raise ValueError(
                f"Subject line {line_number} must use CODE | Title | Units | Day (1-7) | Start (HH:MM) | End (HH:MM)."
            )

        fields = match.groupdict()
        code = fields["code"].strip()
        title = fields["title"].strip()
        if not code or not title:
            raise ValueError(f"Subject line {line_number} needs both a code and a title.")
        if fields["start"] >= fields["end"]:
            raise ValueError(f"Subject line {line_number} must end after its start time.")
        normalized_code = code.casefold()
        if normalized_code in seen_codes:
            raise ValueError(f"Subject code {code} appears more than once.")
        seen_codes.add(normalized_code)
        subjects.append(
            {
                "code": code,
                "title": title,
                "units": int(fields["units"]),
                "day_of_week": int(fields["day"]),
                "start_time": fields["start"],
                "end_time": fields["end"],
            }
        )

    if not subjects:
        raise ValueError("Add at least one subject to make this section available for enrollment.")
    if len(subjects) > 40:
        raise ValueError("A section can have at most 40 subjects.")
    return subjects


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
def courses_sections(request: HttpRequest) -> HttpResponse:
    with connection.cursor() as cursor:
        cursor.execute('SELECT COUNT(DISTINCT "course") FROM "course_sections"')
        course_count = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM "course_sections"')
        section_count = cursor.fetchone()[0]
        paginator = Paginator(range(section_count), PAGE_SIZE)
        page = paginator.get_page(request.GET.get("page"))
        offset = (page.number - 1) * PAGE_SIZE

        cursor.execute(
            'SELECT DISTINCT "course" FROM "course_sections" ORDER BY "course"'
        )
        course_names = [row[0] for row in cursor.fetchall()]
        cursor.execute(
            """
            SELECT s."id", s."course", s."code", s."yearLevel", s."semester",
                   creator."username" AS "creatorUsername",
                   teacher."username" AS "teacherUsername",
                   COALESCE(
                     ARRAY_AGG(
                       DISTINCT (sub."code" || ' - ' || sub."title")
                       ORDER BY (sub."code" || ' - ' || sub."title")
                     ) FILTER (WHERE sub."id" IS NOT NULL),
                     ARRAY[]::text[]
                   ) AS "subjects"
            FROM "course_sections" s
            JOIN "users" creator ON creator."id" = s."createdById"
            LEFT JOIN "users" teacher ON teacher."id" = s."teacherId"
            LEFT JOIN "subjects" sub ON sub."sectionId" = s."id"
            GROUP BY s."id", creator."username", teacher."username"
            ORDER BY s."course", s."yearLevel", s."code"
            LIMIT %s OFFSET %s
            """,
            [PAGE_SIZE, offset],
        )
        sections = [row_as_dict(cursor, row) for row in cursor.fetchall()]
        cursor.execute(
            """
            SELECT "id", "username"
            FROM "users"
            WHERE "role"::text = 'ADMIN'
            ORDER BY "username"
            """
        )
        creators = [row_as_dict(cursor, row) for row in cursor.fetchall()]
        cursor.execute(
            """
            SELECT "id", "username"
            FROM "users"
            WHERE "role"::text = 'TEACHER'
            ORDER BY "username"
            """
        )
        teachers = [row_as_dict(cursor, row) for row in cursor.fetchall()]

    return render(
        request,
        "admissions/courses_sections.html",
        {
            "course_count": course_count,
            "section_count": section_count,
            "course_names": course_names,
            "sections": sections,
            "page": page,
            "creators": creators,
            "teachers": teachers,
        },
    )


@require_POST
@admin_required
def create_course_section(request: HttpRequest) -> HttpResponse:
    course = request.POST.get("course", "").strip()
    section_code = request.POST.get("section_code", "").strip()
    year_level_raw = request.POST.get("year_level", "").strip()
    semester = request.POST.get("semester", "").strip()
    creator_id = request.POST.get("creator_id", "").strip()
    teacher_id = request.POST.get("teacher_id", "").strip() or None

    if not course or len(course) > 120:
        messages.error(request, "Course name is required and must be 120 characters or fewer.")
        return redirect("admissions:courses_sections")
    if not section_code or len(section_code) > 80:
        messages.error(request, "Section code is required and must be 80 characters or fewer.")
        return redirect("admissions:courses_sections")
    if not semester or len(semester) > 80:
        messages.error(request, "Semester is required and must be 80 characters or fewer.")
        return redirect("admissions:courses_sections")
    try:
        year_level = int(year_level_raw)
        uuid.UUID(creator_id)
        if teacher_id:
            uuid.UUID(teacher_id)
        subjects = parse_section_subjects(request.POST.get("subjects", ""))
    except (ValueError, TypeError) as error:
        messages.error(request, str(error) or "Enter valid section and subject details.")
        return redirect("admissions:courses_sections")
    if not 1 <= year_level <= 8:
        messages.error(request, "Year level must be between 1 and 8.")
        return redirect("admissions:courses_sections")

    section_id = str(uuid.uuid4())
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT 1 FROM "users"
                    WHERE "id" = %s AND "role"::text = 'ADMIN'
                    """,
                    [creator_id],
                )
                if cursor.fetchone() is None:
                    messages.error(request, "Choose a valid video-call ADMIN account as the section creator.")
                    return redirect("admissions:courses_sections")

                if teacher_id:
                    cursor.execute(
                        """
                        SELECT 1 FROM "users"
                        WHERE "id" = %s AND "role"::text = 'TEACHER'
                        """,
                        [teacher_id],
                    )
                    if cursor.fetchone() is None:
                        messages.error(request, "Choose a valid teacher account.")
                        return redirect("admissions:courses_sections")

                cursor.execute(
                    'SELECT 1 FROM "course_sections" WHERE "code" = %s',
                    [section_code],
                )
                if cursor.fetchone() is not None:
                    messages.error(request, "That section code is already in use.")
                    return redirect("admissions:courses_sections")

                cursor.execute(
                    """
                    INSERT INTO "course_sections"
                      ("id", "code", "course", "yearLevel", "semester",
                       "createdById", "teacherId", "createdAt")
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                    """,
                    [section_id, section_code, course, year_level, semester, creator_id, teacher_id],
                )
                for subject in subjects:
                    cursor.execute(
                        """
                        INSERT INTO "subjects"
                          ("id", "code", "title", "units", "dayOfWeek",
                           "startTime", "endTime", "sectionId", "createdAt")
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        """,
                        [
                            str(uuid.uuid4()),
                            subject["code"],
                            subject["title"],
                            subject["units"],
                            subject["day_of_week"],
                            subject["start_time"],
                            subject["end_time"],
                            section_id,
                        ],
                    )
    except IntegrityError:
        logger.exception("Could not create course section %s", section_code)
        messages.error(
            request,
            "The course section could not be saved because its code or subject codes conflict with existing records.",
        )
        return redirect("admissions:courses_sections")

    messages.success(request, f"Section {section_code} for {course} was created with {len(subjects)} subjects.")
    return redirect("admissions:courses_sections")


@require_POST
@admin_required
def create_section_subject(request: HttpRequest, section_id: uuid.UUID) -> HttpResponse:
    section_id_text = str(section_id)
    subject_fields = [
        request.POST.get("code", ""),
        request.POST.get("title", ""),
        request.POST.get("units", ""),
        request.POST.get("day_of_week", ""),
        request.POST.get("start_time", ""),
        request.POST.get("end_time", ""),
    ]
    try:
        subject = parse_section_subjects(" | ".join(subject_fields))[0]
    except (ValueError, TypeError) as error:
        messages.error(request, str(error) or "Enter valid subject details.")
        return redirect(f"/admissions/courses-sections/?subject_section={section_id_text}")

    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT "code" FROM "course_sections" WHERE "id" = %s',
                    [section_id_text],
                )
                section = cursor.fetchone()
                if section is None:
                    messages.error(request, "The selected course section no longer exists.")
                    return redirect("admissions:courses_sections")

                cursor.execute(
                    'SELECT COUNT(*) FROM "subjects" WHERE "sectionId" = %s',
                    [section_id_text],
                )
                if cursor.fetchone()[0] >= 40:
                    messages.error(request, "A section can have at most 40 subjects.")
                    return redirect(f"/admissions/courses-sections/?subject_section={section_id_text}")

                cursor.execute(
                    """
                    SELECT 1 FROM "subjects"
                    WHERE "sectionId" = %s AND LOWER("code") = LOWER(%s)
                    """,
                    [section_id_text, subject["code"]],
                )
                if cursor.fetchone() is not None:
                    messages.error(
                        request,
                        f"Subject code {subject['code']} already exists in this section.",
                    )
                    return redirect(f"/admissions/courses-sections/?subject_section={section_id_text}")

                cursor.execute(
                    """
                    INSERT INTO "subjects"
                      ("id", "code", "title", "units", "dayOfWeek",
                       "startTime", "endTime", "sectionId", "createdAt")
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    """,
                    [
                        str(uuid.uuid4()),
                        subject["code"],
                        subject["title"],
                        subject["units"],
                        subject["day_of_week"],
                        subject["start_time"],
                        subject["end_time"],
                        section_id_text,
                    ],
                )
    except IntegrityError:
        logger.exception("Could not add subject %s to section %s", subject["code"], section_id_text)
        messages.error(request, "The subject could not be saved because its code conflicts with an existing record.")
        return redirect(f"/admissions/courses-sections/?subject_section={section_id_text}")

    messages.success(request, f"Subject {subject['code']} was added to section {section[0]}.")
    return redirect("admissions:courses_sections")


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
        student_subjects = {student["id"]: [] for student in students}
        if student_subjects:
            student_placeholders = ", ".join(["%s"] * len(student_subjects))
            cursor.execute(
                f"""
                SELECT enrollment."studentId", subject."id" AS "subjectId",
                       subject."code", subject."title", subject."dayOfWeek",
                       subject."startTime", subject."endTime",
                       section."course", section."code" AS "sectionCode"
                FROM "enrollments" enrollment
                JOIN LATERAL (
                    SELECT subject.*
                    FROM "subjects" subject
                    WHERE subject."id" = enrollment."courseCode"
                       OR EXISTS (
                           SELECT 1
                           FROM "admission_subjects" admission_subject
                           JOIN "admissions" admission
                             ON admission."id" = admission_subject."admissionId"
                           WHERE admission_subject."subjectId" = subject."id"
                             AND (
                                 admission_subject."joinCode" = enrollment."courseCode"
                                 OR subject."id" = enrollment."courseCode"
                                 OR subject."code" = enrollment."courseCode"
                             )
                             AND admission."studentId" = enrollment."studentId"
                             AND admission."status"::text = 'FINALIZED'
                       )
                       OR (
                           subject."code" = enrollment."courseCode"
                           AND (
                               SELECT COUNT(*)
                               FROM "subjects" code_match
                               WHERE code_match."code" = enrollment."courseCode"
                           ) = 1
                       )
                    ORDER BY
                        CASE
                            WHEN EXISTS (
                                SELECT 1
                                FROM "admission_subjects" admission_subject
                                JOIN "admissions" admission
                                  ON admission."id" = admission_subject."admissionId"
                                WHERE admission_subject."subjectId" = subject."id"
                                  AND admission_subject."joinCode" = enrollment."courseCode"
                                  AND admission."studentId" = enrollment."studentId"
                                  AND admission."status"::text = 'FINALIZED'
                            ) THEN 0
                            WHEN subject."id" = enrollment."courseCode" THEN 1
                            WHEN EXISTS (
                                SELECT 1
                                FROM "admission_subjects" admission_subject
                                JOIN "admissions" admission
                                  ON admission."id" = admission_subject."admissionId"
                                WHERE admission_subject."subjectId" = subject."id"
                                  AND subject."code" = enrollment."courseCode"
                                  AND admission."studentId" = enrollment."studentId"
                                  AND admission."status"::text = 'FINALIZED'
                            ) THEN 2
                            ELSE 3
                        END,
                        subject."id"
                    LIMIT 1
                ) subject ON TRUE
                JOIN "course_sections" section ON section."id" = subject."sectionId"
                WHERE enrollment."studentId" IN ({student_placeholders})
                ORDER BY enrollment."studentId", subject."code"
                """,
                list(student_subjects),
            )
            for row in cursor.fetchall():
                subject = row_as_dict(cursor, row)
                subject["dayName"] = WEEKDAY_NAMES.get(subject["dayOfWeek"], "Unknown day")
                student_subjects[subject["studentId"]].append(subject)
        for student in students:
            student["subjects"] = student_subjects[student["id"]]

    return render(
        request,
        "admissions/student_list.html",
        {"students": students, "page": page, "search": search},
    )


def _student_subject_redirect(request: HttpRequest, student_id: uuid.UUID) -> HttpResponse:
    params = {"subject_student": str(student_id)}
    for key in ("q", "page"):
        value = request.POST.get(f"return_{key}", "").strip()
        if value:
            params[key] = value
    return redirect(f"{reverse('admissions:students')}?{urlencode(params)}")


@require_POST
@admin_required
def update_student_subject_time(
    request: HttpRequest,
    student_id: uuid.UUID,
    subject_id: uuid.UUID,
) -> HttpResponse:
    day_of_week_raw = request.POST.get("day_of_week", "").strip()
    start_time = request.POST.get("start_time", "").strip()
    end_time = request.POST.get("end_time", "").strip()
    try:
        day_of_week = int(day_of_week_raw)
    except ValueError:
        day_of_week = 0
    if (
        day_of_week not in WEEKDAY_NAMES
        or
        SUBJECT_TIME.fullmatch(start_time) is None
        or SUBJECT_TIME.fullmatch(end_time) is None
        or start_time >= end_time
    ):
        messages.error(request, "Choose a valid weekday and enter valid subject times, with the end time after the start time.")
        return _student_subject_redirect(request, student_id)

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT subject."code", section."code"
                FROM "subjects" subject
                JOIN "course_sections" section ON section."id" = subject."sectionId"
                WHERE subject."id" = %s
                  AND EXISTS (
                      SELECT 1
                      FROM "enrollments" enrollment
                      WHERE enrollment."studentId" = %s
                        AND (
                            enrollment."courseCode" = subject."id"
                            OR EXISTS (
                                SELECT 1
                                FROM "admission_subjects" admission_subject
                                JOIN "admissions" admission
                                  ON admission."id" = admission_subject."admissionId"
                                WHERE admission_subject."subjectId" = subject."id"
                                  AND (
                                      admission_subject."joinCode" = enrollment."courseCode"
                                      OR subject."code" = enrollment."courseCode"
                                  )
                                  AND admission."studentId" = enrollment."studentId"
                                  AND admission."status"::text = 'FINALIZED'
                            )
                            OR (
                                enrollment."courseCode" = subject."code"
                                AND (
                                    SELECT COUNT(*)
                                    FROM "subjects" code_match
                                    WHERE code_match."code" = enrollment."courseCode"
                                ) = 1
                            )
                        )
                  )
                FOR UPDATE OF subject
                """,
                [str(subject_id), str(student_id)],
            )
            subject = cursor.fetchone()
            if subject is None:
                messages.error(request, "That subject is not enrolled by this student or no longer exists.")
                return _student_subject_redirect(request, student_id)

            cursor.execute(
                """
                UPDATE "subjects"
                SET "dayOfWeek" = %s, "startTime" = %s, "endTime" = %s
                WHERE "id" = %s
                """,
                [day_of_week, start_time, end_time, str(subject_id)],
            )

    messages.success(
        request,
        f"Updated {subject[0]} schedule for section {subject[1]}. Students will see the new schedule after refreshing the mobile app.",
    )
    return _student_subject_redirect(request, student_id)


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
