
from django.core.exceptions import PermissionDenied, ValidationError
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.permissions import (
    HasActiveBusinessContext,
    HasModulePermission,
)
from apps.inventory.serializers import ProductSerializer
from apps.inventory.services.products import (
    create_product,
    deactivate_product,
    get_product_for_business,
    get_products_for_business,
    update_product,
)


class ProductListCreateView(APIView):
    permission_classes = [
        IsAuthenticated,
        HasActiveBusinessContext,
        HasModulePermission,
    ]
    permission_module = "inventory"

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


class ProductDetailView(APIView):
    permission_classes = [
        IsAuthenticated,
        HasActiveBusinessContext,
        HasModulePermission,
    ]
    permission_module = "inventory"

    def get_object(self, request, pk):
        product = get_product_for_business(
            business=request.active_business,
            product_id=pk,
        )

        if not product:
            from rest_framework.exceptions import NotFound
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
