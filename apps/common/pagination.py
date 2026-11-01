from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    """
    Default for every list endpoint:
    ``?page=2&page_size=50`` (default 25, maximum 100). The response has
    ``count``, ``next``, ``previous`` and ``results``.
    """

    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100
