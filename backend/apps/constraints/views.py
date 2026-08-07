from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from backend.apps.access.permissions import ResourcePolicyPermission
from backend.apps.access.resource_policies.constraints import PlanningResourcePolicy, TeacherOwnedResourcePolicy
from backend.apps.constraints.models import (
    CounselorConstraintPreference, CourseConflict, CourseQualificationRequirement,
    CourseRoomRequirement, HardConstraint, Qualification, SoftConstraint,
    TeacherAvailability, TeacherCoursePreference, TeacherCurrentCourse, TeacherQualification,
)
from backend.apps.constraints.serializers import (
    CounselorConstraintPreferenceSerializer, CourseConflictSerializer,
    CourseQualificationRequirementSerializer, CourseRoomRequirementSerializer,
    HardConstraintSerializer, QualificationSerializer, SectionLockSerializer,
    SoftConstraintSerializer, TeacherAvailabilitySerializer, TeacherCoursePreferenceSerializer,
    TeacherCurrentCourseSerializer, TeacherQualificationSerializer,
)
from backend.apps.control.models import SectionLock
from backend.apps.courses.models import Section
from backend.apps.people.models import Teacher


class PolicyFilteredViewSet(viewsets.ModelViewSet):
    permission_classes = [ResourcePolicyPermission]
    filter_fields = ()

    def get_queryset(self):
        queryset = self.resource_policy_class.filter_read_queryset(self.request.user, self.queryset.all())
        for field in self.filter_fields:
            value = self.request.query_params.get(field)
            if value is not None:
                queryset = queryset.filter(**{field: value})
        return queryset


class SharedConstraintViewSet(PolicyFilteredViewSet):
    resource_policy_class = PlanningResourcePolicy


class TeacherNestedViewSet(PolicyFilteredViewSet):
    resource_policy_class = TeacherOwnedResourcePolicy
    filter_fields = ()

    def get_teacher(self):
        if not hasattr(self, "_teacher"):
            self._teacher = get_object_or_404(Teacher.objects.select_related("user"), pk=self.kwargs["teacher_id"])
        return self._teacher

    def get_policy_context(self):
        teacher = Teacher.objects.select_related("user").filter(pk=self.kwargs["teacher_id"]).first()
        return {"teacher_user_id": teacher.user_id if teacher else None}

    def get_queryset(self):
        return super().get_queryset().filter(teacher=self.get_teacher())

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["teacher"] = self.get_teacher()
        return context

    def perform_create(self, serializer):
        serializer.save(teacher=self.get_teacher())


class TeacherQualificationViewSet(TeacherNestedViewSet):
    queryset = TeacherQualification.objects.select_related("teacher__user", "qualification")
    serializer_class = TeacherQualificationSerializer
    filter_fields = ("qualification",)


class TeacherCoursePreferenceViewSet(TeacherNestedViewSet):
    queryset = TeacherCoursePreference.objects.select_related("teacher__user", "course")
    serializer_class = TeacherCoursePreferenceSerializer
    filter_fields = ("course",)


class TeacherCurrentCourseViewSet(TeacherNestedViewSet):
    queryset = TeacherCurrentCourse.objects.select_related("teacher__user", "course", "academic_year")
    serializer_class = TeacherCurrentCourseSerializer
    filter_fields = ("course", "academic_year")


class TeacherAvailabilityViewSet(TeacherNestedViewSet):
    queryset = TeacherAvailability.objects.select_related("teacher__user", "timeslot")
    serializer_class = TeacherAvailabilitySerializer
    filter_fields = ("timeslot",)


class QualificationViewSet(SharedConstraintViewSet):
    queryset = Qualification.objects.all()
    serializer_class = QualificationSerializer


class HardConstraintViewSet(SharedConstraintViewSet):
    queryset = HardConstraint.objects.all()
    serializer_class = HardConstraintSerializer
    filter_fields = ("type",)


class SoftConstraintViewSet(SharedConstraintViewSet):
    queryset = SoftConstraint.objects.all()
    serializer_class = SoftConstraintSerializer
    filter_fields = ("category",)


class CounselorConstraintPreferenceViewSet(SharedConstraintViewSet):
    queryset = CounselorConstraintPreference.objects.select_related("counselor", "constraint")
    serializer_class = CounselorConstraintPreferenceSerializer
    filter_fields = ("counselor", "constraint")


class CourseConflictViewSet(SharedConstraintViewSet):
    queryset = CourseConflict.objects.select_related("course_a", "course_b")
    serializer_class = CourseConflictSerializer
    filter_fields = ("course_a", "course_b")


class CourseRoomRequirementViewSet(SharedConstraintViewSet):
    queryset = CourseRoomRequirement.objects.select_related("course")
    serializer_class = CourseRoomRequirementSerializer
    filter_fields = ("course", "room_type")


class CourseQualificationRequirementViewSet(SharedConstraintViewSet):
    queryset = CourseQualificationRequirement.objects.select_related("course", "qualification")
    serializer_class = CourseQualificationRequirementSerializer
    filter_fields = ("course", "qualification")


class SectionLockView(APIView):
    permission_classes = [ResourcePolicyPermission]
    resource_policy_class = PlanningResourcePolicy

    def get_section(self):
        return get_object_or_404(Section.objects.select_related("course"), pk=self.kwargs["section_id"])

    def get(self, request, section_id):
        lock = get_object_or_404(SectionLock, section=self.get_section())
        return Response(SectionLockSerializer(lock, context={"section": lock.section}).data)

    def patch(self, request, section_id):
        section = self.get_section()
        lock = SectionLock.objects.filter(section=section).first()
        created = lock is None
        if lock is None:
            lock = SectionLock(section=section)
        serializer = SectionLockSerializer(lock, data=request.data, partial=True, context={"section": section})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
