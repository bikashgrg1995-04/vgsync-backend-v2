"""
Stock orders (Branch -> Head Office): input and output shapes.

Quantities come back as strings with three decimals. Every event (dispatch,
receipt, return, return receipt, review) is read-only history.
"""

from decimal import Decimal

from rest_framework import serializers

ZERO = Decimal("0")


def qty(value):
    return f"{Decimal(value or 0):.3f}"


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

class OrderItemInputSerializer(serializers.Serializer):
    product = serializers.IntegerField(min_value=1)
    quantity = serializers.DecimalField(max_digits=14, decimal_places=3, min_value=Decimal("0.001"))


class OrderCreateSerializer(serializers.Serializer):
    items = OrderItemInputSerializer(many=True, allow_empty=False, max_length=200)
    notes = serializers.CharField(required=False, allow_blank=True)


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(allow_blank=False, trim_whitespace=True)


class DispatchItemInputSerializer(serializers.Serializer):
    transfer_item = serializers.IntegerField(min_value=1)
    quantity = serializers.DecimalField(max_digits=14, decimal_places=3, min_value=Decimal("0.001"))


class DispatchSerializer(serializers.Serializer):
    items = DispatchItemInputSerializer(many=True, allow_empty=False)
    notes = serializers.CharField(required=False, allow_blank=True)


class ReceiveItemInputSerializer(serializers.Serializer):
    transfer_item = serializers.IntegerField(min_value=1)
    good_quantity = serializers.DecimalField(
        max_digits=14, decimal_places=3, min_value=Decimal("0"), required=False, default=ZERO
    )
    damaged_quantity = serializers.DecimalField(
        max_digits=14, decimal_places=3, min_value=Decimal("0"), required=False, default=ZERO
    )


class ReceiveSerializer(serializers.Serializer):
    items = ReceiveItemInputSerializer(many=True, allow_empty=False)
    notes = serializers.CharField(required=False, allow_blank=True)


class ReturnSerializer(DispatchSerializer):
    """Damaged stock sent back to Head Office. A note says why."""

    notes = serializers.CharField(allow_blank=False, trim_whitespace=True)


class ReturnReceiveItemSerializer(serializers.Serializer):
    return_item = serializers.IntegerField(min_value=1)
    received_quantity = serializers.DecimalField(max_digits=14, decimal_places=3, min_value=Decimal("0.001"))


class ReturnReceiveSerializer(serializers.Serializer):
    items = ReturnReceiveItemSerializer(many=True, allow_empty=False)
    notes = serializers.CharField(required=False, allow_blank=True)


class ReviewItemSerializer(serializers.Serializer):
    receipt_item = serializers.IntegerField(min_value=1)
    accepted_quantity = serializers.DecimalField(max_digits=14, decimal_places=3, min_value=Decimal("0"))
    rejected_quantity = serializers.DecimalField(max_digits=14, decimal_places=3, min_value=Decimal("0"))
    remarks = serializers.CharField(required=False, allow_blank=True)


class ReviewSerializer(serializers.Serializer):
    items = ReviewItemSerializer(many=True, allow_empty=False)
    remarks = serializers.CharField(allow_blank=False, trim_whitespace=True)


# ---------------------------------------------------------------------------
# Output (built by hand: the totals need the whole history)
# ---------------------------------------------------------------------------

def _user(user):
    if user is None:
        return ""
    return user.get_full_name() or user.username


def _item_name(item):
    return item.product.name


def transfer_summary(transfer, *, totals=None):
    """One row of the list."""
    totals = totals or order_totals(transfer)

    return {
        "id": transfer.pk,
        "reference": transfer.reference,
        "status": transfer.status,
        "branch": transfer.destination_business_id,
        "branch_name": transfer.destination_business.name,
        "head_office": transfer.source_business_id,
        "head_office_name": transfer.source_business.name,
        "notes": transfer.notes,
        "created_by_name": _user(transfer.created_by),
        "created_at": transfer.created_at,
        "updated_at": transfer.updated_at,
        "ordered": qty(totals["ordered"]),
        "dispatched": qty(totals["dispatched"]),
        "received_good": qty(totals["good"]),
        "received_damaged": qty(totals["damaged"]),
        "damaged_to_return": qty(totals["damaged_on_hand"]),
    }


def order_totals(transfer):
    ordered = dispatched = good = damaged = returned = ZERO

    for item in transfer.items.all():
        ordered += item.ordered_quantity
        dispatched += sum((d.quantity for d in item.dispatch_items.all()), ZERO)
        good += sum((r.good_quantity for r in item.receipt_items.all()), ZERO)
        damaged += sum((r.damaged_quantity for r in item.receipt_items.all()), ZERO)
        returned += sum((r.quantity for r in item.return_items.all()), ZERO)

    return {
        "ordered": ordered,
        "dispatched": dispatched,
        "good": good,
        "damaged": damaged,
        "damaged_on_hand": damaged - returned,
    }


def transfer_detail(transfer):
    items = []

    for item in transfer.items.all():
        dispatched = sum((d.quantity for d in item.dispatch_items.all()), ZERO)
        good = sum((r.good_quantity for r in item.receipt_items.all()), ZERO)
        damaged = sum((r.damaged_quantity for r in item.receipt_items.all()), ZERO)
        returned = sum((r.quantity for r in item.return_items.all()), ZERO)

        items.append({
            "id": item.pk,
            "product": item.product_id,
            "stock_no": item.product.stock_no,
            "product_name": item.product.name,
            "unit": item.product.unit,
            "ordered": qty(item.ordered_quantity),
            "dispatched": qty(dispatched),
            "to_dispatch": qty(item.ordered_quantity - dispatched),
            "received_good": qty(good),
            "received_damaged": qty(damaged),
            "to_receive": qty(dispatched - good - damaged),
            "returned": qty(returned),
            "damaged_to_return": qty(damaged - returned),
        })

    dispatches = [
        {
            "id": d.pk,
            "reference": d.reference,
            "notes": d.notes,
            "by": _user(d.dispatched_by),
            "at": d.dispatched_at,
            "items": [
                {"product_name": _item_name(i.transfer_item), "quantity": qty(i.quantity)}
                for i in d.items.all()
            ],
        }
        for d in transfer.dispatches.all()
    ]

    receipts = [
        {
            "id": r.pk,
            "reference": r.reference,
            "notes": r.notes,
            "by": _user(r.received_by),
            "at": r.received_at,
            "items": [
                {
                    "product_name": _item_name(i.transfer_item),
                    "good_quantity": qty(i.good_quantity),
                    "damaged_quantity": qty(i.damaged_quantity),
                }
                for i in r.items.all()
            ],
        }
        for r in transfer.receipts.all()
    ]

    returns = []

    for batch in transfer.returns.all():
        receipt = getattr(batch, "receipt", None)
        review = getattr(receipt, "review", None) if receipt else None

        returns.append({
            "id": batch.pk,
            "reference": batch.reference,
            "notes": batch.notes,
            "by": _user(batch.dispatched_by),
            "at": batch.dispatched_at,
            "items": [
                {
                    "id": i.pk,
                    "product_name": _item_name(i.transfer_item),
                    "quantity": qty(i.quantity),
                }
                for i in batch.items.all()
            ],
            "receipt": None if receipt is None else {
                "id": receipt.pk,
                "reference": receipt.reference,
                "notes": receipt.notes,
                "by": _user(receipt.received_by),
                "at": receipt.received_at,
                "items": [
                    {
                        "id": ri.pk,
                        "return_item": ri.return_item_id,
                        "product_name": _item_name(ri.return_item.transfer_item),
                        "received_quantity": qty(ri.received_quantity),
                    }
                    for ri in receipt.items.all()
                ],
            },
            "review": None if review is None else {
                "id": review.pk,
                "decision": review.decision,
                "remarks": review.remarks,
                "by": _user(review.reviewed_by),
                "at": review.reviewed_at,
                "items": [
                    {
                        "product_name": _item_name(vi.receipt_item.return_item.transfer_item),
                        "accepted_quantity": qty(vi.accepted_quantity),
                        "rejected_quantity": qty(vi.rejected_quantity),
                        "remarks": vi.remarks,
                    }
                    for vi in review.items.all()
                ],
            },
        })

    return {**transfer_summary(transfer), "items": items, "dispatches": dispatches,
            "receipts": receipts, "returns": returns}
