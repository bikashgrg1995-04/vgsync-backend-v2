
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Prefetch

from rest_framework import status
from rest_framework.exceptions import (
    NotFound,
    PermissionDenied,
    ValidationError,
)
from rest_framework.filters import SearchFilter, OrderingFilter
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from django_filters.rest_framework import DjangoFilterBackend

from apps.accounts import rbac
from apps.approvals.models import Kind
from apps.approvals.services import gate
from apps.common.permissions import (
    HasActiveBusinessContext,
    HasModulePermission,
)

from apps.inventory.filters import StockMovementFilter
from apps.inventory.pagination import InventoryMovementPagination

from apps.inventory.models import (
    StockBalance,
    StockMovement,
)

from apps.inventory.serializers import (
    BusinessStockSerializer,
    ProductSerializer,
    StockMovementCreateSerializer,
    StockMovementSerializer,
)

from apps.inventory.services.products import (
    create_product,
    deactivate_product,
    get_product_for_business,
    get_products_for_business,
    update_product,
)

from apps.inventory.services.stock import (
    create_stock_movement,
    create_stock_adjustment,
)


# ==================================================
# BASE INVENTORY VIEW
# ==================================================

class InventoryBaseView(APIView):
    permission_classes = [
        IsAuthenticated,
        HasActiveBusinessContext,
        HasModulePermission,
    ]

    permission_module = "inventory"


# ==================================================
# PRODUCTS
# ==================================================

class ProductListCreateView(InventoryBaseView):

    def get(self, request):
        products = get_products_for_business(
            request.active_business
        )

        serializer = ProductSerializer(
            products,
            many=True,
            context={"request": request},
        )

        return Response(serializer.data)

    def post(self, request):
        serializer = ProductSerializer(
            data=request.data,
            context={"request": request},
        )

        serializer.is_valid(raise_exception=True)

        product = create_product(
            user=request.user,
            business=request.active_business,
            validated_data=serializer.validated_data,
        )

        return Response(
            ProductSerializer(
                product,
                context={"request": request},
            ).data,
            status=status.HTTP_201_CREATED,
        )


class ProductDetailView(InventoryBaseView):

    def get_object(self, request, pk):
        product = get_product_for_business(
            business=request.active_business,
            product_id=pk,
        )

        if not product:
            raise NotFound("Product not found.")

        return product

    def get(self, request, pk):
        product = self.get_object(request, pk)

        serializer = ProductSerializer(
            product,
            context={"request": request},
        )

        return Response(serializer.data)

    def put(self, request, pk):
        return self.update(request, pk, partial=False)

    def patch(self, request, pk):
        return self.update(request, pk, partial=True)

    def update(self, request, pk, *, partial):
        product = self.get_object(request, pk)

        serializer = ProductSerializer(
            product,
            data=request.data,
            partial=partial,
            context={"request": request},
        )

        serializer.is_valid(raise_exception=True)

        updated_product = update_product(
            user=request.user,
            business=request.active_business,
            product_id=pk,
            validated_data=serializer.validated_data,
        )

        return Response(
            ProductSerializer(
                updated_product,
                context={"request": request},
            ).data
        )

    def delete(self, request, pk):
        product = self.get_object(request, pk)

        deactivate_product(
            user=request.user,
            business=request.active_business,
            product_id=product.pk,
        )

        return Response(status=status.HTTP_204_NO_CONTENT)


# ==================================================
# STOCK BALANCES
# ==================================================

class StockLookupPermission(HasModulePermission):
    """
    The product list that sale and purchase forms pick from. Someone who may
    record a sale or a purchase needs it even without ``inventory.view``.
    """

    LOOKUP_CODES = (
        "sales.create", "sales.update", "orders.create",
        "purchases.create", "purchases.update",
    )

    def has_permission(self, request, view):
        if super().has_permission(request, view):
            return True

        user = request.user
        business = getattr(request, "active_business", None)

        if not user or not user.is_authenticated or business is None:
            return False

        if request.method != "GET":
            return False

        mode = getattr(request, "access_mode", "full")

        return any(rbac.has_permission(user, c, mode) for c in self.LOOKUP_CODES)


class StockListView(InventoryBaseView):
    permission_classes = [
        IsAuthenticated,
        HasActiveBusinessContext,
        StockLookupPermission,
    ]

    def get(self, request):
        business = request.active_business

        business_balances = (
            StockBalance.objects
            .filter(business=business)
            .select_related("product")
        )

        products = (
            get_products_for_business(business)
            .prefetch_related(
                Prefetch(
                    "stock_balances",
                    queryset=business_balances,
                    to_attr="active_business_balances",
                )
            )
        )

        serializer = BusinessStockSerializer(
            products,
            many=True,
            context={"request": request}
        )
        data = serializer.data
        mode = getattr(request, "access_mode", "full")

        # Sellers see what to sell and for how much, not what it cost,
        # unless they also buy or manage stock.
        sees_cost = any(
            rbac.has_permission(request.user, c, mode)
            for c in ("inventory.view", "purchases.create", "purchases.update")
        )

        if not sees_cost:
            data = [
                {k: v for k, v in row.items() if k != "purchase_price"}
                for row in data
            ]

        return Response(data)


class StockDetailView(InventoryBaseView):

    def get(self, request, product_id):
        business = request.active_business

        product = get_product_for_business(
            business=business,
            product_id=product_id,
        )

        if not product:
            raise NotFound("Product not found.")

        balance = (
            StockBalance.objects
            .filter(
                product=product,
                business=business,
            )
            .first()
        )

        product.active_business_balances = (
            [balance] if balance else []
        )

        serializer = BusinessStockSerializer(
            product,
            context={"request": request},
        )

        return Response(serializer.data)


# ==================================================
# STOCK MOVEMENTS
# ==================================================

class StockMovementListCreateView(GenericAPIView):

    permission_classes = [
        IsAuthenticated,
        HasActiveBusinessContext,
        HasModulePermission,
    ]

    permission_module = "inventory"

    # Filtering

    filter_backends = [
        DjangoFilterBackend,
        SearchFilter,
        OrderingFilter,
    ]

    filterset_class = StockMovementFilter

    # Search

    search_fields = [
        "reference",
        "notes",
        "stock_balance__product__stock_no",
        "stock_balance__product__name",
    ]

    # Ordering

    ordering_fields = [
        "created_at",
        "quantity",
        "movement_type",
        "stock_condition",
    ]

    ordering = ["-created_at", "-pk"]

    # Pagination

    pagination_class = InventoryMovementPagination

    def get(self, request):
        # Always restrict the queryset to the active business first.
        movements = (
            StockMovement.objects
            .filter(
                stock_balance__business=request.active_business,
            )
            .select_related(
                "stock_balance__product",
                "stock_balance__business",
                "created_by",
            )
        )

        # Apply filters, search and ordering.
        movements = self.filter_queryset(movements)

        # Paginate the filtered queryset.
        page = self.paginate_queryset(movements)

        serializer = StockMovementSerializer(
            page,
            many=True,
        )

        return self.get_paginated_response(serializer.data)

    
    @staticmethod
    def _authorise_adjustment(request, data):
        """Adjusting stock, and writing it off, are their own permissions;
        a large one waits for approval (owner-set threshold, by value)."""
        write_off = data["adjustment_action"] == "remove_damaged"
        code = "inventory.write_off" if write_off else "inventory.adjust"

        if not rbac.has_permission(
            request.user, code, request.access_mode
        ):
            raise PermissionDenied("You do not have permission to do this.")

        price = getattr(data["product"], "purchase_price", None) or 0
        value = Decimal(data["quantity"]) * Decimal(price)

        gate(
            request,
            Kind.STOCK_WRITE_OFF if write_off else Kind.STOCK_ADJUSTMENT,
            value,
            f"{data['adjustment_action']} {data['quantity']} x {data['product']}",
        )

    def post(self, request):
        serializer = StockMovementCreateSerializer(
            data=request.data,
            context={"request": request},
        )

        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data

        try:
            if data["movement_type"] in {
                StockMovement.MovementType.ADJUSTMENT_IN,
                StockMovement.MovementType.ADJUSTMENT_OUT,
            }:
                self._authorise_adjustment(request, data)

                movement = create_stock_adjustment(
                    user=request.user,
                    business=request.active_business,
                    product=data["product"],
                    action=data["adjustment_action"],
                    quantity=data["quantity"],
                    reason=data["adjustment_reason"],
                    reference=data["reference"],
                    notes=data["notes"],
                )

            else:
                movement = create_stock_movement(
                    user=request.user,
                    business=request.active_business,
                    product=data["product"],
                    movement_type=data["movement_type"],
                    quantity=data["quantity"],
                    stock_condition=data["stock_condition"],
                    reference=data["reference"],
                    notes=data["notes"],
                )

        except DjangoValidationError as exc:
            detail = (
                exc.message_dict
                if hasattr(exc, "message_dict")
                else exc.messages
            )

            raise ValidationError(detail) from exc

        return Response(
            StockMovementSerializer(movement).data,
            status=status.HTTP_201_CREATED,
        )



# ==================================================
# STOCK MOVEMENT DETAIL
# ==================================================

class StockMovementDetailView(InventoryBaseView):

    def get(self, request, pk):
        movement = (
            StockMovement.objects
            .filter(
                pk=pk,
                stock_balance__business=request.active_business,
            )
            .select_related(
                "stock_balance__product",
                "stock_balance__business",
                "created_by",
            )
            .first()
        )

        if not movement:
            raise NotFound("Stock movement not found.")

        serializer = StockMovementSerializer(movement)

        return Response(serializer.data)
