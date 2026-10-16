from django.urls import path

from . import views

app_name = "admissions"

urlpatterns = [
    path("", views.admission_list, name="list"),
    path("students/", views.student_list, name="students"),
    path("teachers/", views.teacher_list, name="teachers"),
    path("teachers/create/", views.create_teacher, name="create_teacher"),
    path("teachers/create/submit/", views.create_teacher_submit, name="create_teacher_submit"),
    path("sections/assign-teachers/", views.assign_teachers, name="assign_teachers"),
    path("sections/assign-teachers/save/", views.assign_teacher_submit, name="assign_teacher_submit"),
    path("<str:admission_id>/", views.admission_detail, name="detail"),
    path("<str:admission_id>/review/", views.review_admission, name="review"),
]
