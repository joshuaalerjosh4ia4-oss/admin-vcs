from contextlib import nullcontext
import bcrypt
import socket
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import AnonymousUser, User
from django.core import mail
from django.core.servers.basehttp import WSGIRequestHandler
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings

from .management.commands.runserver import AutoPortWSGIServer
from .notifications import send_admission_confirmation
from .views import admin_required, assign_teacher_submit, create_teacher_submit, review_admission


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


class AdmissionNotificationTests(SimpleTestCase):
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
        self.assertIn("Monday, 08:00-09:00 (Morning)", message.body)
        self.assertIn("Video meeting IDs and passcodes are shared separately", message.body)
