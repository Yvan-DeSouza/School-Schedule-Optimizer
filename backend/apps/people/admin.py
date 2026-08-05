from django.contrib import admin

from backend.apps.people.models import Counselor, Student, Teacher, UserRoleProfile


admin.site.register([Student, Teacher, Counselor, UserRoleProfile])
