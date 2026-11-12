from django.db.models import Count, Q
from django.utils import timezone
from datetime import timedelta
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts import rbac
from apps.accounts.scope import scoped_users
from apps.common.permissions import HasActiveBusinessContext

from . import services
from .models import Task
from .serializers import (
    NoteSerializer,
    StatusSerializer,
    TaskEditSerializer,
    TaskSerializer,
    TaskWriteSerializer,
    person,
)


class TaskViewSet(viewsets.ModelViewSet):
    """
    Tasks handed to the team.

    Everyone sees the tasks assigned to them and moves them along
    (``status/``, ``comment/``). Seeing everyone's tasks needs ``tasks.view``,
    assigning needs ``tasks.create``, editing or reassigning ``tasks.update``
    and cancelling ``tasks.delete``. The owner has all of them.

    Filters: ``mine=true``, ``assigned_by_me=true``, ``status`` (comma
    separated), ``assigned_to``, ``overdue=true``, ``search``.
    """

    permission_classes = [IsAuthenticated, HasActiveBusinessContext]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    search_fields = ("title", "description")

    # -- who may do what ----------------------------------------------------

    def _can(self, code):
        request = self.request

        return rbac.has_permission(
            request.user, code, getattr(request, "access_mode", "full")
        )

    def _need(self, code):
        if not self._can(code):
            raise PermissionDenied("You do not have permission to do this.")

    def _writable(self):
        if getattr(self.request, "access_mode", "full") == "read_only":
            raise PermissionDenied("This business is read-only for you.")

    def _is_involved(self, task):
        user = self.request.user

        return user.pk in (task.assigned_to_id, task.assigned_by_id)

    # -- data ---------------------------------------------------------------

    def get_queryset(self):
        user = self.request.user
        queryset = Task.objects.filter(
            business=self.request.active_business
        ).select_related("assigned_to", "assigned_by", "assigned_to__position")

        if not self._can("tasks.view"):
            queryset = queryset.filter(Q(assigned_to=user) | Q(assigned_by=user))

        params = self.request.query_params

        if params.get("mine") == "true":
            queryset = queryset.filter(assigned_to=user)

        if params.get("assigned_by_me") == "true":
            queryset = queryset.filter(assigned_by=user)

        if params.get("status"):
            queryset = queryset.filter(status__in=params["status"].split(","))

        if params.get("assigned_to"):
            queryset = queryset.filter(assigned_to_id=params["assigned_to"])

        if params.get("overdue") == "true":
            queryset = queryset.filter(
                status__in=[Task.Status.TODO, Task.Status.IN_PROGRESS],
                due_date__lt=timezone.localdate(),
            )

        search = params.get("search")

        if search:
            queryset = queryset.filter(
                Q(title__icontains=search) | Q(description__icontains=search)
            )

        return queryset

    def get_serializer_class(self):
        return TaskSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["with_events"] = self.action in (
            "retrieve", "create", "partial_update", "status", "comment", "cancel"
        )

        return context

    def _assignee(self, user_id):
        assignee = (
            scoped_users(self.request.active_business)
            .filter(pk=user_id, is_active=True, can_sign_in=True)
            .first()
        )

        if assignee is None:
            raise ValidationError({"assigned_to": "Choose a team member of this business."})

        return assignee

    def _fresh(self, task):
        task = self.get_queryset().get(pk=task.pk)

        return Response(self.get_serializer(task).data)

    # -- actions ------------------------------------------------------------

    def create(self, request, *args, **kwargs):
        self._writable()
        self._need("tasks.create")

        data = TaskWriteSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        fields = dict(data.validated_data)
        assignee = self._assignee(fields.pop("assigned_to"))

        task = services.create_task(
            request.active_business, request.user, assignee, **fields
        )

        return Response(
            self._fresh(task).data, status=status.HTTP_201_CREATED
        )

    def partial_update(self, request, *args, **kwargs):
        self._writable()
        self._need("tasks.update")
        task = self.get_object()
        data = TaskEditSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        changes = dict(data.validated_data)

        if "assigned_to" in changes:
            changes["assigned_to"] = self._assignee(changes["assigned_to"])

        services.edit_task(task, request.user, changes)

        return self._fresh(task)

    def destroy(self, request, *args, **kwargs):
        # Tasks keep their history; cancelling is the way to drop one.
        return Response(
            {"detail": "Cancel the task instead."},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    @action(detail=True, methods=["post"])
    def status(self, request, pk=None):
        self._writable()
        task = self.get_object()
        data = StatusSerializer(data=request.data)
        data.is_valid(raise_exception=True)

        is_assignee = task.assigned_to_id == request.user.pk

        if not (is_assignee or self._can("tasks.update")):
            raise PermissionDenied("Only the person it is assigned to can do this.")

        if task.status == Task.Status.CANCELLED and not self._can("tasks.update"):
            raise PermissionDenied("This task was cancelled.")

        services.change_status(
            task, request.user, data.validated_data["status"], data.validated_data["note"]
        )

        return self._fresh(task)

    @action(detail=True, methods=["post"])
    def comment(self, request, pk=None):
        self._writable()
        task = self.get_object()
        data = NoteSerializer(data=request.data)
        data.is_valid(raise_exception=True)

        services.add_comment(task, request.user, data.validated_data["note"])

        return self._fresh(task)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        self._writable()
        self._need("tasks.delete")
        task = self.get_object()

        if task.status == Task.Status.DONE:
            raise ValidationError({"detail": "A finished task cannot be cancelled."})

        note = request.data.get("note", "") if hasattr(request.data, "get") else ""
        services.change_status(task, request.user, Task.Status.CANCELLED, str(note)[:500])

        return self._fresh(task)

    @action(detail=False, methods=["get"])
    def assignees(self, request):
        """Who a task can be handed to."""
        self._need("tasks.create")
        # Someone who never signs in cannot be handed work.
        users = scoped_users(request.active_business).filter(is_active=True, can_sign_in=True).select_related("position")

        return Response([person(u) for u in users.order_by("first_name", "username")])

    @action(detail=False, methods=["get"])
    def summary(self, request):
        today = timezone.localdate()
        week = timezone.now() - timedelta(days=7)
        open_states = [Task.Status.TODO, Task.Status.IN_PROGRESS]
        base = Task.objects.filter(business=request.active_business)

        def counts(queryset):
            return {
                "open": queryset.filter(status__in=open_states).count(),
                "overdue": queryset.filter(status__in=open_states, due_date__lt=today).count(),
                "done_this_week": queryset.filter(
                    status=Task.Status.DONE, completed_at__gte=week
                ).count(),
            }

        result = {"mine": counts(base.filter(assigned_to=request.user))}

        if self._can("tasks.view"):
            result["team"] = counts(base)
            rows = (
                base.exclude(status=Task.Status.CANCELLED)
                .values("assigned_to", "assigned_to__first_name", "assigned_to__last_name", "assigned_to__username")
                .annotate(
                    open=Count("id", filter=Q(status__in=open_states)),
                    overdue=Count("id", filter=Q(status__in=open_states, due_date__lt=today)),
                    done=Count("id", filter=Q(status=Task.Status.DONE)),
                )
                .order_by("-open")
            )
            result["people"] = [
                {
                    "id": r["assigned_to"],
                    "name": f"{r['assigned_to__first_name']} {r['assigned_to__last_name']}".strip()
                    or r["assigned_to__username"],
                    "open": r["open"],
                    "overdue": r["overdue"],
                    "done": r["done"],
                }
                for r in rows
            ]

        return Response(result)
