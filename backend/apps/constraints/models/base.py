from django.db import models


class HardConstraint(models.Model):
    name = models.CharField(max_length=200)

    type = models.CharField(max_length=100)
    # conflict, prerequisite, qualification, capacity

    priority = models.IntegerField(default=100)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class SoftConstraint(models.Model):
    name = models.CharField(max_length=200)

    category = models.CharField(max_length=100)
    # balance_semesters, teacher_preferences, group_size_balance

    default_weight = models.IntegerField(default=1)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class CounselorConstraintPreference(models.Model):
    counselor = models.ForeignKey("people.Counselor", on_delete=models.CASCADE)

    constraint = models.ForeignKey(SoftConstraint, on_delete=models.CASCADE)

    weight = models.IntegerField(default=1)

    class Meta:
        ordering = ["counselor", "constraint"]

    def __str__(self):
        return f"{self.counselor} prefers {self.constraint} at {self.weight}"


class Qualification(models.Model):
    name = models.CharField(
        max_length=100,
        unique=True
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
