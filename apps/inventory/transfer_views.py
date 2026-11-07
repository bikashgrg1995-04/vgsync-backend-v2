"""
Stock orders: a Branch orders from its Head Office, Head Office dispatches
good stock, the Branch checks what arrived (good and damaged apart) and can
send damaged stock back with a note; Head Office receives and reviews it.

A business only sees the orders it is part of: a Branch sees its own,
Head Office sees those from its branches. The services enforce who may do
what (only a Branch orders, receives and returns; only Head Office
dispatches and reviews) and keep the stock correct.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Prefetch, Q

from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.filters import SearchFilter
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.pagination import StandardPagination
from apps.common.permissions import HasActiveBusinessContext, HasModulePermission

from apps.inventory.models import (
    StockTransfer,
    StockTransferDispatch,
    StockTransferReceipt,
    StockTransferReturn,
)
from apps.inventory.services.transfers.cancel import cancel_stock_transfer_order
from apps.inventory.services.transfers.dispatch import dispatch_stock_transfer
from apps.inventory.services.transfers.orders import create_stock_transfer_order
from apps.inventory.services.transfers.receipts import receive_stock_transfer
from apps.inventory.services.transfers.return_receipts import (
    receive_stock_transfer_return,
)
from apps.inventory.services.transfers.returns import return_damaged_stock_transfer
from apps.inventory.services.transfers.review import (
    _determine_review_decision,
    review_stock_transfer_return,
)
from apps.inventory.transfer_serializers import (
    DispatchSerializer,
    OrderCreateSerializer,
    ReasonSerializer,
    ReceiveSerializer,
    ReturnReceiveSerializer,
    ReturnSerializer,
    ReviewSerializer,
    transfer_detail,
    transfer_summary,
)

ZERO = Decimal("0")


def run(service, **kwargs):
    """Call a service; its validation errors become a 400."""
    try:
        return service(**kwargs)
    except DjangoValidationError as exc:
        detail = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        raise ValidationError(detail) from exc


def visible_transfers(business):
    return (
        StockTransfer.objects
        .filter(Q(source_business=business) | Q(destination_business=business))
        .select_related("source_business", "destination_business", "created_by")
        .prefetch_related(
            "items__product",
            "items__dispatch_items",
            "items__receipt_items",
            "items__return_items",
            Prefetch(
                "dispatches",
                queryset=StockTransferDispatch.objects.select_related("dispatched_by")
                .prefetch_related("items__transfer_item__product").order_by("pk"),
            ),
            Prefetch(
                "receipts",
                queryset=StockTransferReceipt.objects.select_related("received_by")
                .prefetch_related("items__transfer_item__product").order_by("pk"),
            ),
            Prefetch(
                "returns",
                queryset=StockTransferReturn.objects.select_related("dispatched_by")
                .prefetch_related(
                    "items__transfer_item__product",
                    "receipt__received_by",
                    "receipt__items__return_item__transfer_item__product",
                    "receipt__review__reviewed_by",
                    "receipt__review__items__receipt_item__return_item__transfer_item__product",
                ).order_by("pk"),
            ),
        )
    )


def get_transfer(request, pk):
    transfer = visible_transfers(request.active_business).filter(pk=pk).first()

    if transfer is None:
        raise NotFound("Order not found.")

    return transfer


def next_reference(prefix, count):
    return f"{prefix}-{count + 1}"


class TransferBaseView(GenericAPIView):
    permission_classes = [IsAuthenticated, HasActiveBusinessContext, HasModulePermission]
    permission_module = "inventory"


class TransferListCreateView(TransferBaseView):
    filter_backends = [SearchFilter]
    search_fields = ["reference", "notes", "items__product__name", "items__product__stock_no"]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = visible_transfers(self.request.active_business).distinct()
        wanted = self.request.query_params.get("status")

        if wanted:
            qs = qs.filter(status__in=wanted.split(","))

        return qs

    def get(self, request):
        page = self.paginate_queryset(self.filter_queryset(self.get_queryset()))
        return self.get_paginated_response([transfer_summary(t) for t in page])

    def post(self, request):
        serializer = OrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        branch = request.active_business
        taken = StockTransfer.objects.filter(destination_business=branch).count()
        reference = f"ORD-{branch.business_code}-{taken + 1:04d}"

        while StockTransfer.objects.filter(reference=reference).exists():
            taken += 1
            reference = f"ORD-{branch.business_code}-{taken + 1:04d}"

        transfer = run(
            create_stock_transfer_order,
            user=request.user,
            branch=branch,
            items=[
                {"product_id": i["product"], "quantity": i["quantity"]}
                for i in serializer.validated_data["items"]
            ],
            reference=reference,
            notes=serializer.validated_data.get("notes", ""),
        )

        return Response(
            transfer_detail(get_transfer(request, transfer.pk)),
            status=status.HTTP_201_CREATED,
        )


class TransferDetailView(TransferBaseView):
    def get(self, request, pk):
        return Response(transfer_detail(get_transfer(request, pk)))


class TransferCancelView(TransferBaseView):
    def post(self, request, pk):
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        get_transfer(request, pk)
        run(
            cancel_stock_transfer_order,
            user=request.user,
            business=request.active_business,
            transfer_id=pk,
            reason=serializer.validated_data["reason"],
        )

        return Response(transfer_detail(get_transfer(request, pk)))


class TransferDispatchView(TransferBaseView):
    def post(self, request, pk):
        serializer = DispatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        transfer = get_transfer(request, pk)
        run(
            dispatch_stock_transfer,
            user=request.user,
            transfer_id=transfer.pk,
            items=[
                {"transfer_item_id": i["transfer_item"], "quantity": i["quantity"]}
                for i in serializer.validated_data["items"]
            ],
            reference=next_reference(f"{transfer.reference}-D", transfer.dispatches.count()),
            notes=serializer.validated_data.get("notes", ""),
        )

        return Response(transfer_detail(get_transfer(request, pk)))


class TransferReceiveView(TransferBaseView):
    def post(self, request, pk):
        serializer = ReceiveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        transfer = get_transfer(request, pk)
        run(
            receive_stock_transfer,
            user=request.user,
            transfer_id=transfer.pk,
            items=[
                {
                    "transfer_item_id": i["transfer_item"],
                    "good_quantity": i["good_quantity"],
                    "damaged_quantity": i["damaged_quantity"],
                }
                for i in serializer.validated_data["items"]
            ],
            reference=next_reference(f"{transfer.reference}-R", transfer.receipts.count()),
            notes=serializer.validated_data.get("notes", ""),
        )

        return Response(transfer_detail(get_transfer(request, pk)))


class TransferReturnView(TransferBaseView):
    def post(self, request, pk):
        serializer = ReturnSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        transfer = get_transfer(request, pk)
        run(
            return_damaged_stock_transfer,
            user=request.user,
            transfer_id=transfer.pk,
            items=[
                {"transfer_item_id": i["transfer_item"], "quantity": i["quantity"]}
                for i in serializer.validated_data["items"]
            ],
            reference=next_reference(f"{transfer.reference}-X", transfer.returns.count()),
            notes=serializer.validated_data["notes"],
        )

        return Response(transfer_detail(get_transfer(request, pk)))


class TransferReturnReceiveView(TransferBaseView):
    def post(self, request, pk, batch_id):
        serializer = ReturnReceiveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        transfer = get_transfer(request, pk)

        if not transfer.returns.filter(pk=batch_id).exists():
            raise NotFound("Return not found.")

        run(
            receive_stock_transfer_return,
            user=request.user,
            return_batch_id=batch_id,
            items=[
                {"return_item_id": i["return_item"], "received_quantity": i["received_quantity"]}
                for i in serializer.validated_data["items"]
            ],
            reference=f"{transfer.reference}-XR{batch_id}",
            notes=serializer.validated_data.get("notes", ""),
        )

        return Response(transfer_detail(get_transfer(request, pk)))


class TransferReturnReviewView(TransferBaseView):
    def post(self, request, pk, batch_id):
        serializer = ReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        transfer = get_transfer(request, pk)
        batch = transfer.returns.filter(pk=batch_id).first()

        if batch is None:
            raise NotFound("Return not found.")

        receipt = getattr(batch, "receipt", None)

        if receipt is None:
            raise ValidationError("Receive the returned goods before reviewing them.")

        items = serializer.validated_data["items"]
        accepted = sum((i["accepted_quantity"] for i in items), ZERO)
        rejected = sum((i["rejected_quantity"] for i in items), ZERO)

        run(
            review_stock_transfer_return,
            user=request.user,
            receipt_id=receipt.pk,
            decision=_determine_review_decision(accepted, rejected),
            items=[
                {
                    "receipt_item_id": i["receipt_item"],
                    "accepted_quantity": i["accepted_quantity"],
                    "rejected_quantity": i["rejected_quantity"],
                    "remarks": i.get("remarks", ""),
                }
                for i in items
            ],
            remarks=serializer.validated_data["remarks"],
        )

        return Response(transfer_detail(get_transfer(request, pk)))
