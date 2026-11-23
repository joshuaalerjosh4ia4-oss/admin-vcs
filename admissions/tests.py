from contextlib import nullcontext
import bcrypt
import socket
import uuid
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User
from django.core import mail
from django.core.servers.basehttp import WSGIRequestHandler
from django.http import HttpResponse
from django.template import Context, Template
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import resolve

from .management.commands.runserver import AutoPortWSGIServer
from .notifications import send_admission_confirmation
from .time_format import format_time_12_hour
from .views import (
    admin_required,
    assign_teacher_submit,
    create_course_section,
    create_section_subject,
    create_teacher_submit,
    courses_sections,
    parse_section_subjects,
    review_admission,
    student_list,
    update_student_subject_time,
)


class AutoPortServerTests(SimpleTestCase):
    def test_server_uses_next_port_when_requested_port_is_occupied(self):
        blocker = socket.socket()
        server = None
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        requested_port = blocker.getsockname()[1]

        try:
            server = AutoPortWSGIServer(
                ("127.0.0.1", requested_port),
                WSGIRequestHandler,
            )
            self.assertEqual(server.server_port, requested_port + 1)
            self.assertEqual(server.requested_port, requested_port)
        finally:
            if server is not None:
                server.server_close()
            blocker.close()


class AdminAccessTests(SimpleTestCase):
    databases = {"default"}

    def setUp(self):
        self.factory = RequestFactory()
        self.protected_view = admin_required(lambda request: HttpResponse("ok"))

    def test_anonymous_user_is_redirected_to_login(self):
        request = self.factory.get("/admissions/")
        request.user = AnonymousUser()

        response = self.protected_view(request)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/login/"))

    def test_staff_user_without_superuser_privileges_is_forbidden(self):
        request = self.factory.get("/admissions/")
        request.user = User(username="staff", is_active=True, is_staff=True, is_superuser=False)

        response = self.protected_view(request)

        self.assertEqual(response.status_code, 403)

    def test_superuser_can_access_protected_view(self):
        request = self.factory.get("/admissions/")
        request.user = User(username="admin", is_active=True, is_staff=True, is_superuser=True)

        response = self.protected_view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")

    def test_root_page_renders_public_landing_page(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Every learner has a place to")
        self.assertContains(response, "Admissions staff sign in")

    def test_student_and_teacher_directories_require_admin_access(self):
        students_response = self.client.get("/admissions/students/")
        teachers_response = self.client.get("/admissions/teachers/")

        self.assertEqual(students_response.status_code, 302)
        self.assertEqual(teachers_response.status_code, 302)
        self.assertTrue(students_response["Location"].startswith("/login/"))
        self.assertTrue(teachers_response["Location"].startswith("/login/"))

    def test_student_list_shows_clickable_student_subjects_and_times(self):
        student_id = "08c89f3f-f653-4faf-86d1-03009f505f41"
        subject_id = "4679448b-c26a-447f-9dc8-877099c0f399"
        request = self.factory.get("/admissions/students/")
        request.user = User(username="academic-admin", is_active=True, is_staff=True, is_superuser=True)
        request.resolver_match = resolve("/admissions/students/")
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = (1,)
        cursor.fetchall.side_effect = [
            [(student_id, "student-one", "student@example.com", None, "Information Technology", 1, "BSIT-1A")],
            [(student_id, subject_id, "IT101", "Introduction to Computing", 1, "08:00", "10:00", "Information Technology", "BSIT-1A")],
        ]

        def set_query_description(sql, parameters=None):
            if 'FROM "users" u' in sql and 'SELECT u."id"' in sql:
                cursor.description = [
                    ("id",), ("username",), ("email",), ("createdAt",),
                    ("course",), ("yearLevel",), ("sectionCode",),
                ]
            elif 'FROM "enrollments" enrollment' in sql:
                cursor.description = [
                    ("studentId",), ("subjectId",), ("code",), ("title",),
                    ("dayOfWeek",), ("startTime",), ("endTime",), ("course",), ("sectionCode",),
                ]

        cursor.execute.side_effect = set_query_description
        with patch("admissions.views.connection.cursor", return_value=cursor):
            response = student_list(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="student-row" tabindex="0"', html=False)
        self.assertContains(response, f'data-open-student-modal="student-modal-{student_id}"', html=False)
        self.assertContains(response, f'aria-label="View profile for student-one"', html=False)
        self.assertContains(response, "Introduction to Computing")
        self.assertContains(response, "Monday")
        self.assertContains(response, "8:00 AM–10:00 AM")
        self.assertContains(
            response,
            f'action="/admissions/students/{student_id}/subjects/{subject_id}/time/"',
            html=False,
        )

    def test_update_student_subject_time_persists_times(self):
        student_id = "08c89f3f-f653-4faf-86d1-03009f505f41"
        subject_id = "4679448b-c26a-447f-9dc8-877099c0f399"
        request = self.factory.post(
            f"/admissions/students/{student_id}/subjects/{subject_id}/time/",
            {"day_of_week": "2", "start_time": "09:30", "end_time": "11:00"},
        )
        request.user = User(username="admin", is_active=True, is_staff=True, is_superuser=True)
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = ("IT101", "BSIT-1A")

        with patch("admissions.views.connection.cursor", return_value=cursor), patch(
            "admissions.views.transaction.atomic", return_value=nullcontext()
        ), patch("admissions.views.messages.success") as success:
            response = update_student_subject_time(
                request,
                uuid.UUID(student_id),
                uuid.UUID(subject_id),
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"/admissions/students/?subject_student={student_id}",
        )
        update_sql, update_parameters = cursor.execute.call_args_list[1].args
        self.assertIn('UPDATE "subjects"', update_sql)
        self.assertEqual(update_parameters, [2, "09:30", "11:00", subject_id])
        success.assert_called_once()

    def test_update_student_subject_time_rejects_invalid_time_without_database_write(self):
        student_id = "08c89f3f-f653-4faf-86d1-03009f505f41"
        subject_id = "4679448b-c26a-447f-9dc8-877099c0f399"
        request = self.factory.post(
            f"/admissions/students/{student_id}/subjects/{subject_id}/time/",
            {"day_of_week": "2", "start_time": "11:00", "end_time": "10:00"},
        )
        request.user = User(username="admin", is_active=True, is_staff=True, is_superuser=True)

        with patch("admissions.views.connection.cursor") as cursor, patch(
            "admissions.views.messages.error"
        ) as error:
            response = update_student_subject_time(
                request,
                uuid.UUID(student_id),
                uuid.UUID(subject_id),
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"/admissions/students/?subject_student={student_id}",
        )
        cursor.assert_not_called()
        error.assert_called_once()

    def test_update_student_subject_time_rejects_subject_not_enrolled_by_student(self):
        student_id = "08c89f3f-f653-4faf-86d1-03009f505f41"
        subject_id = "4679448b-c26a-447f-9dc8-877099c0f399"
        request = self.factory.post(
            f"/admissions/students/{student_id}/subjects/{subject_id}/time/",
            {"day_of_week": "2", "start_time": "09:00", "end_time": "10:00"},
        )
        request.user = User(username="admin", is_active=True, is_staff=True, is_superuser=True)
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = None

        with patch("admissions.views.connection.cursor", return_value=cursor), patch(
            "admissions.views.transaction.atomic", return_value=nullcontext()
        ), patch("admissions.views.messages.error") as error:
            response = update_student_subject_time(
                request,
                uuid.UUID(student_id),
                uuid.UUID(subject_id),
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"/admissions/students/?subject_student={student_id}",
        )
        self.assertEqual(cursor.execute.call_count, 1)
        error.assert_called_once()

    def test_update_student_subject_time_rejects_invalid_weekday(self):
        student_id = "08c89f3f-f653-4faf-86d1-03009f505f41"
        subject_id = "4679448b-c26a-447f-9dc8-877099c0f399"
        request = self.factory.post(
            f"/admissions/students/{student_id}/subjects/{subject_id}/time/",
            {"day_of_week": "8", "start_time": "09:00", "end_time": "10:00"},
        )
        request.user = User(username="admin", is_active=True, is_staff=True, is_superuser=True)

        with patch("admissions.views.connection.cursor") as cursor, patch(
            "admissions.views.messages.error"
        ) as error:
            response = update_student_subject_time(
                request,
                uuid.UUID(student_id),
                uuid.UUID(subject_id),
            )

        self.assertEqual(response.status_code, 302)
        cursor.assert_not_called()
        error.assert_called_once()

    def test_superuser_sees_admissions_student_and_teacher_navigation(self):
        admin = User.objects.create_superuser(
            username="school-admin",
            email="school-admin@example.com",
            password="test-password",
        )
        self.client.force_login(admin)

        response = self.client.get("/")

        self.assertContains(response, "Admissions")
        self.assertContains(response, "Students")
        self.assertContains(response, "Teachers")
        self.assertContains(response, 'href="/admissions/students/"')
        self.assertContains(response, 'href="/admissions/teachers/"')
        self.assertContains(response, "Create teacher account")
        self.assertContains(response, "Assign teachers to sections")
        self.assertContains(response, 'id="admin-logout-form"', html=False)
        self.assertContains(response, 'data-idle-logout-url="/logout/idle/"', html=False)
        self.assertContains(response, "const awayTimeout = 5 * 60 * 1000;")
        self.assertContains(response, "logoutForm.requestSubmit();")

    def test_idle_logout_redirects_to_login_with_expiration_alert(self):
        admin = User.objects.create_superuser(
            username="idle-admin",
            email="idle-admin@example.com",
            password="test-password",
        )
        self.client.force_login(admin)

        response = self.client.post("/logout/idle/", follow=True)

        self.assertContains(response, "Your admin session expired.")
        self.assertFalse(response.context["user"].is_authenticated)

    def test_admin_session_uses_rolling_fourteen_day_cookie(self):
        admin = User.objects.create_superuser(
            username="session-admin",
            email="session-admin@example.com",
            password="test-password",
        )
        self.client.force_login(admin)

        response = self.client.get("/")

        self.assertEqual(settings.SESSION_COOKIE_AGE, 14 * 24 * 60 * 60)
        self.assertEqual(settings.ADMIN_SESSION_IDLE_TIMEOUT_SECONDS, 5 * 60)
        self.assertEqual(settings.ADMIN_SESSION_ABSOLUTE_TIMEOUT_SECONDS, 14 * 24 * 60 * 60)
        self.assertTrue(settings.SESSION_SAVE_EVERY_REQUEST)
        self.assertFalse(settings.SESSION_EXPIRE_AT_BROWSER_CLOSE)
        self.assertEqual(response.cookies[settings.SESSION_COOKIE_NAME]["max-age"], settings.SESSION_COOKIE_AGE)

    def test_section_subject_parser_validates_and_normalizes_rows(self):
        subjects = parse_section_subjects(
            " IT101 | Introduction to Computing | 3 | 1 | 08:00 | 10:00 \n"
            "MATH101|College Mathematics|4|2|10:00|12:00"
        )

        self.assertEqual(len(subjects), 2)
        self.assertEqual(subjects[0]["code"], "IT101")
        self.assertEqual(subjects[0]["day_of_week"], 1)
        self.assertEqual(subjects[1]["units"], 4)

    def test_section_subject_parser_rejects_invalid_schedule_and_duplicate_code(self):
        with self.assertRaisesRegex(ValueError, "end after its start time"):
            parse_section_subjects("IT101|Intro|3|1|10:00|09:00")
        with self.assertRaisesRegex(ValueError, "appears more than once"):
            parse_section_subjects(
                "IT101|Intro|3|1|08:00|09:00\nit101|Intro II|3|2|09:00|10:00"
            )

    def test_create_course_section_writes_to_existing_prisma_tables(self):
        creator_id = "08c89f3f-f653-4faf-86d1-03009f505f41"
        teacher_id = "4679448b-c26a-447f-9dc8-877099c0f399"
        request = self.factory.post(
            "/admissions/courses-sections/create/",
            {
                "course": "Bachelor of Science in IT",
                "section_code": "BSIT-1A",
                "year_level": "1",
                "semester": "First Semester",
                "creator_id": creator_id,
                "teacher_id": teacher_id,
                "subjects": "IT101|Introduction to Computing|3|1|08:00|10:00",
            },
        )
        request.user = User(username="admin", is_active=True, is_staff=True, is_superuser=True)
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.side_effect = [(1,), (1,), None]

        with patch("admissions.views.connection.cursor", return_value=cursor), patch(
            "admissions.views.transaction.atomic", return_value=nullcontext()
        ), patch("admissions.views.messages.success") as success:
            response = create_course_section(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/admissions/courses-sections/")
        section_sql, section_parameters = cursor.execute.call_args_list[3].args
        self.assertIn('INSERT INTO "course_sections"', section_sql)
        self.assertEqual(
            section_parameters[1:7],
            ["BSIT-1A", "Bachelor of Science in IT", 1, "First Semester", creator_id, teacher_id],
        )
        subject_sql, subject_parameters = cursor.execute.call_args_list[4].args
        self.assertIn('INSERT INTO "subjects"', subject_sql)
        self.assertEqual(subject_parameters[1:8], ["IT101", "Introduction to Computing", 3, 1, "08:00", "10:00", section_parameters[0]])
        success.assert_called_once()

    def test_courses_sections_page_renders_creation_form_inside_modal(self):
        request = self.factory.get("/admissions/courses-sections/")
        request.user = User(username="academic-admin", is_active=True, is_staff=True, is_superuser=True)
        request.resolver_match = resolve("/admissions/courses-sections/")
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.side_effect = [(0,), (0,)]
        section_id = "08c89f3f-f653-4faf-86d1-03009f505f41"
        cursor.fetchall.side_effect = [
            [],
            [(section_id, "Bachelor of Science in IT", "BSIT-1A", 1, "First Semester", "admin", None, ["IT101 - Intro"])],
            [],
            [],
        ]
        cursor.description = [
            ("id",),
            ("course",),
            ("code",),
            ("yearLevel",),
            ("semester",),
            ("creatorUsername",),
            ("teacherUsername",),
            ("subjects",),
        ]

        with patch("admissions.views.connection.cursor", return_value=cursor):
            response = courses_sections(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<dialog class="academic-dialog" id="course-modal"', html=False)
        self.assertContains(response, 'id="open-course-modal"', html=False)
        self.assertContains(response, 'id="close-course-modal"', html=False)
        self.assertContains(response, 'id="cancel-course-modal"', html=False)
        self.assertContains(response, 'id="subject-count"', html=False)
        self.assertContains(response, 'action="/admissions/courses-sections/create/"', html=False)
        self.assertContains(response, "Add subject")
        self.assertContains(
            response,
            f'action="/admissions/courses-sections/{section_id}/subjects/create/"',
            html=False,
        )
        self.assertContains(response, "Add course or section")

    def test_create_course_section_requires_a_valid_subject(self):
        request = self.factory.post(
            "/admissions/courses-sections/create/",
            {
                "course": "Course",
                "section_code": "SEC-1",
                "year_level": "1",
                "semester": "First Semester",
                "creator_id": "08c89f3f-f653-4faf-86d1-03009f505f41",
                "subjects": "",
            },
        )
        request.user = User(username="admin", is_active=True, is_staff=True, is_superuser=True)

        with patch("admissions.views.connection.cursor") as cursor, patch(
            "admissions.views.messages.error"
        ):
            response = create_course_section(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/admissions/courses-sections/")
        cursor.assert_not_called()

    def test_create_section_subject_writes_to_the_selected_section(self):
        section_id = "08c89f3f-f653-4faf-86d1-03009f505f41"
        request = self.factory.post(
            f"/admissions/courses-sections/{section_id}/subjects/create/",
            {
                "code": "IT102",
                "title": "Programming Fundamentals",
                "units": "3",
                "day_of_week": "2",
                "start_time": "08:00",
                "end_time": "10:00",
            },
        )
        request.user = User(username="admin", is_active=True, is_staff=True, is_superuser=True)
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.side_effect = [("BSIT-1A",), (0,), None]

        with patch("admissions.views.connection.cursor", return_value=cursor), patch(
            "admissions.views.transaction.atomic", return_value=nullcontext()
        ), patch("admissions.views.messages.success") as success:
            response = create_section_subject(request, uuid.UUID(section_id))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/admissions/courses-sections/")
        subject_sql, subject_parameters = cursor.execute.call_args_list[3].args
        self.assertIn('INSERT INTO "subjects"', subject_sql)
        self.assertEqual(
            subject_parameters[1:],
            ["IT102", "Programming Fundamentals", 3, 2, "08:00", "10:00", section_id],
        )
        success.assert_called_once()

    def test_create_section_subject_rejects_case_insensitive_duplicate_code(self):
        section_id = "08c89f3f-f653-4faf-86d1-03009f505f41"
        request = self.factory.post(
            f"/admissions/courses-sections/{section_id}/subjects/create/",
            {
                "code": "it101",
                "title": "Introduction to Computing",
                "units": "3",
                "day_of_week": "1",
                "start_time": "08:00",
                "end_time": "10:00",
            },
        )
        request.user = User(username="admin", is_active=True, is_staff=True, is_superuser=True)
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.side_effect = [("BSIT-1A",), (1,), (1,)]

        with patch("admissions.views.connection.cursor", return_value=cursor), patch(
            "admissions.views.transaction.atomic", return_value=nullcontext()
        ), patch("admissions.views.messages.error") as error:
            response = create_section_subject(request, uuid.UUID(section_id))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"/admissions/courses-sections/?subject_section={section_id}",
        )
        self.assertEqual(cursor.execute.call_count, 3)
        error.assert_called_once()

    def test_create_section_subject_enforces_section_subject_limit(self):
        section_id = "08c89f3f-f653-4faf-86d1-03009f505f41"
        request = self.factory.post(
            f"/admissions/courses-sections/{section_id}/subjects/create/",
            {
                "code": "IT141",
                "title": "Elective",
                "units": "3",
                "day_of_week": "1",
                "start_time": "08:00",
                "end_time": "10:00",
            },
        )
        request.user = User(username="admin", is_active=True, is_staff=True, is_superuser=True)
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.side_effect = [("BSIT-1A",), (40,)]

        with patch("admissions.views.connection.cursor", return_value=cursor), patch(
            "admissions.views.transaction.atomic", return_value=nullcontext()
        ), patch("admissions.views.messages.error") as error:
            response = create_section_subject(request, uuid.UUID(section_id))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"/admissions/courses-sections/?subject_section={section_id}",
        )
        self.assertEqual(cursor.execute.call_count, 2)
        error.assert_called_once()

    def test_create_teacher_sends_a_bcrypt_compatible_temporary_password(self):
        request = self.factory.post(
            "/admissions/teachers/create/submit/",
            {"username": "teacher-one", "email": "teacher@example.com"},
        )
        request.user = User(username="admin", is_active=True, is_staff=True, is_superuser=True)
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = None

        with patch("admissions.views.connection.cursor", return_value=cursor), patch(
            "admissions.views.transaction.atomic", return_value=nullcontext()
        ), patch("admissions.views.send_mail") as send_teacher_email, patch(
            "admissions.views.messages.success"
        ):
            response = create_teacher_submit(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/admissions/teachers/")
        insert_query, insert_parameters = cursor.execute.call_args_list[1].args
        self.assertIn("'TEACHER'::\"UserRole\"", insert_query)
        self.assertIn('"mustChangePassword"', insert_query)
        self.assertEqual(insert_parameters[1:3], ["teacher-one", "teacher@example.com"])
        temporary_password = send_teacher_email.call_args.kwargs["message"].split(
            "temporary password:\n\n", 1
        )[1].split("\n\n", 1)[0]
        self.assertTrue(
            bcrypt.checkpw(
                temporary_password.encode("utf-8"),
                insert_parameters[3].encode("utf-8"),
            )
        )
        send_teacher_email.assert_called_once()

    def test_assign_teacher_updates_the_selected_course_section(self):
        section_id = "08c89f3f-f653-4faf-86d1-03009f505f41"
        teacher_id = "4679448b-c26a-447f-9dc8-877099c0f399"
        request = self.factory.post(
            "/admissions/sections/assign-teachers/save/",
            {"section_id": section_id, "teacher_id": teacher_id},
        )
        request.user = User(username="admin", is_active=True, is_staff=True, is_superuser=True)
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = (1,)
        cursor.rowcount = 1

        with patch("admissions.views.connection.cursor", return_value=cursor), patch(
            "admissions.views.messages.success"
        ):
            response = assign_teacher_submit(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/admissions/sections/assign-teachers/")
        self.assertIn('"role"::text = \'TEACHER\'', cursor.execute.call_args_list[0].args[0])
        self.assertIn('SET "teacherId" = %s', cursor.execute.call_args_list[1].args[0])
        self.assertEqual(cursor.execute.call_args_list[1].args[1], [teacher_id, section_id])

    def test_acceptance_requires_payment_confirmation(self):
        request = self.factory.post("/admissions/test/review/", {"action": "accept"})
        request.user = User(username="admin", is_active=True, is_staff=True, is_superuser=True)

        with patch("admissions.views.connection.cursor") as cursor, patch(
            "admissions.views.messages.error"
        ):
            response = review_admission(request, admission_id="test")

        self.assertEqual(response.status_code, 302)
        cursor.assert_not_called()

    def test_acceptance_records_payment_and_audit_entry(self):
        admission_id = "08c89f3f-f653-4faf-86d1-03009f505f41"
        request = self.factory.post(
            f"/admissions/{admission_id}/review/",
            {"action": "accept", "confirm": "yes"},
        )
        request.user = User(username="admin", is_active=True, is_staff=True, is_superuser=True)
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = (
            "SUBMITTED", "student", "section", "Course", 1, "1", "student-name",
            "student@example.com", "SECTION-1",
        )
        cursor.fetchall.return_value = []
        cursor.rowcount = 1

        with patch("admissions.views.connection.cursor", return_value=cursor), patch(
            "admissions.views.transaction.atomic", return_value=nullcontext()
        ), patch("admissions.views.AdmissionReviewLog.objects.create") as create_log, patch(
            "admissions.views.messages.success"
        ), patch("admissions.views.send_admission_confirmation") as send_confirmation:
            response = review_admission(request, admission_id=admission_id)

        self.assertEqual(response.status_code, 302)
        update_sql = cursor.execute.call_args_list[1].args[0]
        self.assertIn('"paymentStatus" = \'PAID\'', update_sql)
        self.assertIn('"status" = \'FINALIZED\'', update_sql)
        self.assertIn('"sectionId" = %s', update_sql)
        self.assertNotIn('"codesSentAt" = NOW()', update_sql)
        send_confirmation.assert_called_once_with(
            email="student@example.com",
            username="student-name",
            course="Course",
            section_code="SECTION-1",
            year_level=1,
            semester="1",
            subjects=[],
        )
        create_log.assert_called_once_with(
            admission_id=admission_id,
            admin=request.user,
            action="ACCEPTED",
            reason="",
        )

    def test_email_failure_rolls_back_the_admission_finalization(self):
        admission_id = "08c89f3f-f653-4faf-86d1-03009f505f41"
        request = self.factory.post(
            f"/admissions/{admission_id}/review/",
            {"action": "accept", "confirm": "yes"},
        )
        request.user = User(username="admin", is_active=True, is_staff=True, is_superuser=True)
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = (
            "SUBMITTED", "student", "section", "Course", 1, "1", "student-name",
            "student@example.com", "SECTION-1",
        )
        cursor.fetchall.return_value = []
        cursor.rowcount = 1

        with patch("admissions.views.connection.cursor", return_value=cursor), patch(
            "admissions.views.transaction.atomic", return_value=nullcontext()
        ), patch("admissions.views.transaction.set_rollback") as set_rollback, patch(
            "admissions.views.AdmissionReviewLog.objects.create"
        ) as create_log, patch("admissions.views.messages.error") as error_message, patch(
            "admissions.views.send_admission_confirmation", side_effect=OSError("SMTP unavailable")
        ):
            response = review_admission(request, admission_id=admission_id)

        self.assertEqual(response.status_code, 302)
        set_rollback.assert_called_once_with(True)
        error_message.assert_called_once()
        create_log.assert_not_called()

    def test_acceptance_can_assign_another_section_and_move_matching_subjects(self):
        admission_id = "08c89f3f-f653-4faf-86d1-03009f505f41"
        request = self.factory.post(
            f"/admissions/{admission_id}/review/",
            {"action": "accept", "confirm": "yes", "section_id": "target-section"},
        )
        request.user = User(username="admin", is_active=True, is_staff=True, is_superuser=True)
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.side_effect = [
            (
                "SUBMITTED", "student", "source-section", "Course", 1, "1",
                "student-name", "student@example.com", "SOURCE-1",
            ),
            ("target-section", "TARGET-1"),
            None,
        ]
        cursor.fetchall.side_effect = [
            [("source-subject", "MATH101")],
            [("target-subject", "MATH101")],
            [("MATH101", "Math", 1, "08:00", "09:00", "morning")],
        ]
        cursor.rowcount = 1

        with patch("admissions.views.connection.cursor", return_value=cursor), patch(
            "admissions.views.transaction.atomic", return_value=nullcontext()
        ), patch("admissions.views.AdmissionReviewLog.objects.create"), patch(
            "admissions.views.messages.success"
        ), patch("admissions.views.send_admission_confirmation") as send_confirmation:
            response = review_admission(request, admission_id=admission_id)

        self.assertEqual(response.status_code, 302)
        admission_update = cursor.execute.call_args_list[5]
        self.assertIn('"sectionId" = %s', admission_update.args[0])
        self.assertEqual(admission_update.args[1], ["target-section", admission_id])
        subject_update = cursor.execute.call_args_list[6]
        self.assertEqual(
            subject_update.args[1],
            ["target-subject", admission_id, "source-subject"],
        )
        send_confirmation.assert_called_once_with(
            email="student@example.com",
            username="student-name",
            course="Course",
            section_code="TARGET-1",
            year_level=1,
            semester="1",
            subjects=[("MATH101", "Math", 1, "08:00", "09:00", "morning")],
        )

    def test_acceptance_rejects_a_section_outside_the_course_year_and_semester(self):
        admission_id = "08c89f3f-f653-4faf-86d1-03009f505f41"
        request = self.factory.post(
            f"/admissions/{admission_id}/review/",
            {"action": "accept", "confirm": "yes", "section_id": "unrelated-section"},
        )
        request.user = User(username="admin", is_active=True, is_staff=True, is_superuser=True)
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.side_effect = [
            (
                "SUBMITTED", "student", "source-section", "Course", 1, "1",
                "student-name", "student@example.com", "SOURCE-1",
            ),
            None,
        ]

        with patch("admissions.views.connection.cursor", return_value=cursor), patch(
            "admissions.views.transaction.atomic", return_value=nullcontext()
        ), patch("admissions.views.messages.error"):
            response = review_admission(request, admission_id=admission_id)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(cursor.execute.call_count, 2)

    def test_rejection_requires_reason_and_records_it(self):
        admission_id = "08c89f3f-f653-4faf-86d1-03009f505f41"
        request = self.factory.post(
            f"/admissions/{admission_id}/review/",
            {"action": "reject", "confirm": "yes", "reason": "Incomplete documents"},
        )
        request.user = User(username="admin", is_active=True, is_staff=True, is_superuser=True)
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = (
            "SUBMITTED", "student", "section", "Course", 1, "1", "student-name",
            "student@example.com", "SECTION-1",
        )
        cursor.rowcount = 1

        with patch("admissions.views.connection.cursor", return_value=cursor), patch(
            "admissions.views.transaction.atomic", return_value=nullcontext()
        ), patch("admissions.views.AdmissionReviewLog.objects.create") as create_log, patch(
            "admissions.views.messages.success"
        ):
            response = review_admission(request, admission_id=admission_id)

        self.assertEqual(response.status_code, 302)
        update_sql = cursor.execute.call_args_list[1].args[0]
        self.assertIn('"status" = \'REJECTED\'', update_sql)
        create_log.assert_called_once_with(
            admission_id=admission_id,
            admin=request.user,
            action="REJECTED",
            reason="Incomplete documents",
        )


class AdminSessionTimeoutTests(TestCase):
    def setUp(self):
        admin = User.objects.create_superuser(
            username="timeout-admin",
            email="timeout-admin@example.com",
            password="test-password-that-is-long-enough",
        )
        self.client.force_login(admin)

    def test_admin_session_expires_after_five_minutes_of_server_observed_inactivity(self):
        with patch("admissions.middleware.time.time", return_value=1_000):
            response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session["admin_session_started_at"], 1_000)
        self.assertEqual(self.client.session["admin_session_last_activity"], 1_000)

        with patch("admissions.middleware.time.time", return_value=1_301):
            response = self.client.get("/admissions/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/login/?session_expired=1")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_admin_session_expires_at_its_absolute_lifetime(self):
        with patch("admissions.middleware.time.time", return_value=1_000):
            self.client.get("/")

        session = self.client.session
        session["admin_session_last_activity"] = 1_001
        session.save()

        with override_settings(ADMIN_SESSION_ABSOLUTE_TIMEOUT_SECONDS=1), patch(
            "admissions.middleware.time.time",
            return_value=1_002,
        ):
            response = self.client.get("/admissions/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/login/?session_expired=1")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_admin_activity_refreshes_server_idle_timestamp(self):
        with patch("admissions.middleware.time.time", return_value=1_000):
            self.client.get("/")
        with patch("admissions.middleware.time.time", return_value=1_200):
            response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session["admin_session_started_at"], 1_000)
        self.assertEqual(self.client.session["admin_session_last_activity"], 1_200)


class AdmissionNotificationTests(SimpleTestCase):
    def test_schedule_times_are_formatted_as_12_hour_times(self):
        self.assertEqual(format_time_12_hour("13:00"), "1:00 PM")
        self.assertEqual(format_time_12_hour("00:05"), "12:05 AM")
        self.assertEqual(format_time_12_hour("12:00"), "12:00 PM")
        self.assertEqual(format_time_12_hour("invalid"), "invalid")
        rendered = Template("{% load time_format %}{{ value|time12 }}").render(
            Context({"value": "13:00"})
        )
        self.assertEqual(rendered, "1:00 PM")

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        EMAIL_HOST_USER="mailer@gmail.com",
        EMAIL_HOST_PASSWORD="app-password",
        DEFAULT_FROM_EMAIL="mailer@gmail.com",
    )
    def test_confirmation_contains_finalized_section_and_subject_schedule(self):
        send_admission_confirmation(
            email="student@example.com",
            username="student-name",
            course="Computer Science",
            section_code="CS-1A",
            year_level=1,
            semester="First Semester",
            subjects=[("CS101", "Introduction to Computing", "482019", 1, "08:00", "09:00", "morning")],
        )

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["student@example.com"])
        self.assertEqual(message.from_email, "mailer@gmail.com")
        self.assertIn("Computer Science", message.body)
        self.assertIn("CS-1A", message.body)
        self.assertIn("482019", message.body)
        self.assertIn("Monday, 8:00 AM-9:00 AM (Morning)", message.body)
        self.assertIn("Video meeting IDs and passcodes are shared separately", message.body)
