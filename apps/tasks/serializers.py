from django.utils import timezone
from rest_framework import serializers

from .models import Task, TaskEvent


def person(user):
    return {
        "id": user.pk,
        "name": user.get_full_name() or user.username,
        "position": user.position.name if getattr(user, "position_id", None) else "",
    }


class TaskEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskEvent
        fields = (
            "id",
            "kind",
            "actor_name",
            "from_status",
            "to_status",
            "note",
            "created_at",
        )


class TaskSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    priority_label = serializers.CharField(source="get_priority_display", read_only=True)
    assigned_to = serializers.SerializerMethodField()
    assigned_by = serializers.SerializerMethodField()
    is_overdue = serializers.SerializerMethodField()
    events = TaskEventSerializer(many=True, read_only=True)

    class Meta:
        model = Task
        fields = (
            "id",
            "title",
            "description",
            "status",
            "status_label",
            "priority",
            "priority_label",
            "due_date",
            "is_overdue",
            "assigned_to",
            "assigned_by",
            "started_at",
            "completed_at",
            "created_at",
            "updated_at",
            "events",
        )
        read_only_fields = fields

    def get_assigned_to(self, obj):
        return person(obj.assigned_to)

    def get_assigned_by(self, obj):
        return person(obj.assigned_by)

    def get_is_overdue(self, obj):
        return bool(obj.is_open and obj.due_date and obj.due_date < timezone.localdate())

    def to_representation(self, instance):
        data = super().to_representation(instance)

        # The history is only sent when one task is opened.
        if not self.context.get("with_events"):
            data.pop("events", None)

        return data


class TaskWriteSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200)
    description = serializers.CharField(allow_blank=True, required=False)
    priority = serializers.ChoiceField(choices=Task.Priority.choices, required=False)
    due_date = serializers.DateField(allow_null=True, required=False)
    assigned_to = serializers.IntegerField()


class TaskEditSerializer(TaskWriteSerializer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for field in self.fields.values():
            field.required = False


class StatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=[
            Task.Status.TODO,
            Task.Status.IN_PROGRESS,
            Task.Status.DONE,
        ]
    )
    note = serializers.CharField(max_length=500, allow_blank=True, required=False, default="")


class NoteSerializer(serializers.Serializer):
    note = serializers.CharField(max_length=500)
