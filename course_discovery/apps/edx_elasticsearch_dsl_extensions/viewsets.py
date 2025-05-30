import json

from django_elasticsearch_dsl import Document as OriginDocument
from django_elasticsearch_dsl_drf.filter_backends import DefaultOrderingFilterBackend
from django_elasticsearch_dsl_drf.pagination import PageNumberPagination
from django_elasticsearch_dsl_drf.viewsets import DocumentViewSet as OriginDocumentViewSet
from rest_framework.permissions import IsAuthenticated
from rest_framework.utils.urls import replace_query_param

from course_discovery.apps.api import mixins
from course_discovery.apps.edx_elasticsearch_dsl_extensions.backends import MultiMatchSearchFilterBackend
from course_discovery.apps.edx_elasticsearch_dsl_extensions.search import FacetedSearch


class MultiDocumentsWrapper:
    """
    Multi document wrapper.

    Should be used during implementation a django elasticsearch-dsl-drf document viewset.
    Implements a proxy pattern.
    Provides linking(wrapping) of several elasticsearch documents,
    which from the point of view of elasticsearch-dsl-drf document viewset
    behave as one document.
    """

    def __init__(self, *documents, current_attr=''):
        assert all(
            issubclass(doc, OriginDocument) for doc in documents
        ), '`documents` must be a list of Document subclasses'
        self._documents = documents
        self.current_attr = current_attr

    def _get_using(self):
        # it's okay to get the first one cause all indices use common connection
        return self._documents[0]._get_using()  # pylint: disable=protected-access

    @property
    def _fields(self):
        fields = {}
        for doc in self._documents:
            fields.update(doc._fields)
        return fields

    def dispatch_attr(self, attr):
        current_attr = '{}{}'.format(self.current_attr and '{}.'.format(self.current_attr), attr)
        # pylint: disable=protected-access
        dispatchers = {
            '_index._name': lambda: [doc._index._name for doc in self._documents],
            '_doc_type.mapping.properties.name': lambda: [
                doc._doc_type.mapping.properties.name for doc in self._documents
            ],
            '_doc_type.name': lambda: [doc._doc_type.name for doc in self._documents],
        }

        if not any(k.startswith(current_attr) for k in dispatchers):
            raise AttributeError(attr)

        return dispatchers.get(current_attr, lambda: self.__class__(*self._documents, current_attr=current_attr))()

    def __getattr__(self, attr):
        """
        Supported attributes by name are:

           - lineno - returns the line number of the exception text
           - col - returns the column number of the exception text
           - line - returns the line containing the exception text
        """
        if attr not in ('_doc_type', '_index') and not self.current_attr:
            raise AttributeError(attr)

        return self.dispatch_attr(attr)


class DocumentViewSet(OriginDocumentViewSet):
    """
    Custom document viewset.

    Extends the original document viewset to provide extended `FacetedSearch` class.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.search = FacetedSearch(using=self.client, index=self.index, doc_type=self.document._doc_type.name)

    def get_queryset(self):
        """Get queryset."""
        queryset = self.search.query()
        if isinstance(self.document, OriginDocument):
            queryset.model = self.document.Django.model

        return queryset


class CustomPageNumberPagination(PageNumberPagination):
    """
    Custom page number paginator.

    This is needed in order to make page size customization possible.
    """

    page_size_query_param = 'page_size'


class SearchAfterPagination(PageNumberPagination):
    """
    Custom paginator that supports Elasticsearch `search_after` pagination.
    """

    page_size_query_param = "page_size"
    search_after_param = "search_after"

    def paginate_queryset(self, queryset, request, view=None):
        """
        Paginate the Elasticsearch queryset using search_after.
        """
        search_after = request.query_params.get(self.search_after_param)
        if search_after:
            try:
                queryset = queryset.extra(search_after=json.loads(search_after))
            except json.JSONDecodeError as exc:
                raise ValueError("Invalid JSON format for search_after parameter") from exc
        queryset = super().paginate_queryset(queryset, request, view)
        self.last_obj = queryset[-1] if queryset else None  # pylint: disable=attribute-defined-outside-init
        return queryset

    def get_next_link(self):
        if not self.page.has_next():
            return None

        last_item_sort = self.last_obj.meta.sort.copy() if hasattr(self, 'last_obj') and self.last_obj else None
        if not last_item_sort:
            return None

        url = self.request.build_absolute_uri()
        return replace_query_param(url, self.search_after_param, json.dumps(last_item_sort))


class BaseElasticsearchDocumentViewSet(mixins.DetailMixin, mixins.FacetMixin, DocumentViewSet):
    lookup_field = 'key'
    document_uid_field = 'key'
    pagination_class = CustomPageNumberPagination
    permission_classes = (IsAuthenticated,)
    ensure_published = True
    multi_match_search_fields = ('key', 'title', 'text')
    multi_match_options = {
        'type': 'phrase',
    }
    ordering = ('-start', 'aggregation_key')
    filter_backends = [
        MultiMatchSearchFilterBackend,
        DefaultOrderingFilterBackend,
    ]

    def filter_facet_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        if self.ensure_published:
            # Ensure we only return published, non-hidden items
            queryset = queryset.filter('term', published=True).exclude('term', hidden=True)

        return queryset
