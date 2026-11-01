
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Prefetch

from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.filters import SearchFilter, OrderingFilter
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from django_filters.rest_framework import DjangoFilterBackend

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

from apps.inventory.services.stock import create_stock_movement


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

class StockListView(InventoryBaseView):

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
        )

        return Response(serializer.data)


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

        serializer = BusinessStockSerializer(product)

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

    def post(self, request):
        serializer = StockMovementCreateSerializer(
            data=request.data,
            context={"request": request},
        )

        serializer.is_valid(raise_exception=True)

        try:
            movement = create_stock_movement(
                user=request.user,
                business=request.active_business,
                **serializer.validated_data,
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
