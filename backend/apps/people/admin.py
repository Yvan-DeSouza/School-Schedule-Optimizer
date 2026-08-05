from django.contrib import admin

from backend.apps.people.models import Counselor, Student, Teacher


admin.site.register([Student, Teacher, Counselor])
