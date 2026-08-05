from django.db import models


class Translation(models.Model):
    key = models.CharField(max_length=200, unique=True)

    english = models.TextField()

    french = models.TextField()

    context = models.CharField(max_length=100, null=True, blank=True)
    # ui_label, course_name, error_message

    class Meta:
        ordering = ["key"]

    def __str__(self):
        return self.key
