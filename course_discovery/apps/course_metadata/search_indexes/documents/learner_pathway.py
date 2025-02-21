from django.conf import settings
from django.db.models import Prefetch
from django_elasticsearch_dsl import Index, fields

from course_discovery.apps.course_metadata.choices import CourseRunStatus
from course_discovery.apps.course_metadata.models import CourseRun
from course_discovery.apps.learner_pathway.choices import PathwayStatus
from course_discovery.apps.learner_pathway.models import LearnerPathway

from .analyzers import edge_ngram_completion, synonym_text
from .common import BaseDocument, OrganizationsMixin

__all__ = ('LearnerPathwayDocument',)

LEARNER_PATHWAY_INDEX_NAME = settings.ELASTICSEARCH_INDEX_NAMES[__name__]
LEARNER_PATHWAY_INDEX = Index(LEARNER_PATHWAY_INDEX_NAME)
LEARNER_PATHWAY_INDEX.settings(number_of_shards=1, number_of_replicas=1, blocks={'read_only_allow_delete': None})


@LEARNER_PATHWAY_INDEX.doc_type
class LearnerPathwayDocument(BaseDocument, OrganizationsMixin):
    """
    LearnerPathway Elasticsearch document.
    """
    created = fields.DateField()
    title = fields.TextField(
        analyzer=synonym_text,
        fields={
            'suggest': fields.CompletionField(),
            'edge_ngram_completion': fields.TextField(analyzer=edge_ngram_completion),
        },
    )
    visible_via_association = fields.BooleanField()
    status = fields.TextField()
    overview = fields.TextField()
    published = fields.BooleanField()
    skill_names = fields.KeywordField(multi=True)
    skills = fields.NestedField(properties={
        'name': fields.TextField(),
        'description': fields.TextField(),
    })

    def prepare_aggregation_key(self, obj):
        return 'learnerpathway:{}'.format(obj.uuid)

    def prepare_aggregation_uuid(self, obj):
        return 'learnerpathway:{}'.format(obj.uuid)

    def prepare_published(self, obj):
        return obj.status == PathwayStatus.Active

    def get_queryset(self, excluded_restriction_types=None):
        if excluded_restriction_types is None:
            excluded_restriction_types = []

        course_runs = CourseRun.objects.filter(
            status=CourseRunStatus.Published
        ).exclude(
            restricted_run__restriction_type__in=excluded_restriction_types
        )

        return super().get_queryset().prefetch_related(
            'steps',
            Prefetch(
                'steps__learnerpathwaycourse_set__course__course_runs',
                queryset=course_runs
            ),
            Prefetch(
                'steps__learnerpathwayprogram_set__program__courses__course_runs',
                queryset=course_runs
            )
        )

    def prepare_skill_names(self, obj):
        return [skill['name'] for skill in obj.skills]

    def prepare_skills(self, obj):
        return obj.skills

    class Django:
        """
        Django Elasticsearch DSL ORM Meta.
        """

        model = LearnerPathway

    class Meta:
        """
        Meta options.
        """

        parallel_indexing = True
        queryset_pagination = settings.ELASTICSEARCH_DSL_QUERYSET_PAGINATION
