from django.db.models import Count, Q

from backend.apps.courses.models import CourseRequest


def get_course_demand_summary(academic_year_id):
    """Return per-course request totals for an academic year."""
    rows = (
        CourseRequest.objects.filter(academic_year_id=academic_year_id)
        .values("course_id", "course__course_code", "course__name")
        .annotate(
            primary_requests=Count("id", filter=Q(request_type="primary")),
            alternate_requests=Count("id", filter=Q(request_type="alternate")),
            total_requests=Count("id"),
        )
        .order_by("course__course_code")
    )
    return [
        {
            "course_id": row["course_id"],
            "course_code": row["course__course_code"],
            "course_name": row["course__name"],
            "primary_requests": row["primary_requests"],
            "alternate_requests": row["alternate_requests"],
            "total_requests": row["total_requests"],
        }
        for row in rows
    ]
