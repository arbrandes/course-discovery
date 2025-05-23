"""
Unit tests for CSV Data loader.
"""
import copy
import datetime
from decimal import Decimal
from tempfile import NamedTemporaryFile
from unittest import mock

import responses
from ddt import data, ddt, unpack
from edx_toggles.toggles.testutils import override_waffle_switch
from pytz import UTC
from testfixtures import LogCapture

from course_discovery.apps.api.v1.tests.test_views.mixins import APITestCase, OAuth2Mixin
from course_discovery.apps.core.tests.factories import USER_PASSWORD, UserFactory
from course_discovery.apps.course_metadata.choices import (
    CourseRunStatus, ExternalCourseMarketingType, ExternalProductStatus
)
from course_discovery.apps.course_metadata.data_loaders.constants import CSVIngestionErrorMessages, CSVIngestionErrors
from course_discovery.apps.course_metadata.data_loaders.csv_loader import CSVDataLoader
from course_discovery.apps.course_metadata.data_loaders.tests import mock_data
from course_discovery.apps.course_metadata.data_loaders.tests.mixins import CSVLoaderMixin
from course_discovery.apps.course_metadata.data_loaders.tests.test_utils import MockExceptionWithResponse
from course_discovery.apps.course_metadata.models import (
    AdditionalMetadata, Course, CourseEntitlement, CourseRun, CourseType, Seat, Source, TaxiForm
)
from course_discovery.apps.course_metadata.tests.factories import (
    AdditionalMetadataFactory, CourseFactory, CourseRunFactory, CourseTypeFactory, OrganizationFactory, SourceFactory
)
from course_discovery.apps.course_metadata.toggles import (
    IS_COURSE_RUN_VARIANT_ID_EDITABLE, IS_SUBDIRECTORY_SLUG_FORMAT_ENABLED,
    IS_SUBDIRECTORY_SLUG_FORMAT_FOR_EXEC_ED_ENABLED
)

LOGGER_PATH = 'course_discovery.apps.course_metadata.data_loaders.csv_loader'
MIXIN_LOGGER_PATH = 'course_discovery.apps.course_metadata.data_loaders.mixins'


@ddt
@mock.patch(
    'course_discovery.apps.course_metadata.data_loaders.configured_jwt_decode_handler',
    return_value={'preferred_username': 'test_username'}
)
class TestCSVDataLoader(CSVLoaderMixin, OAuth2Mixin, APITestCase):
    """
    Test Suite for CSVDataLoader.
    """
    def setUp(self) -> None:
        super().setUp()
        self.mock_access_token()
        self.user = UserFactory.create(username="test_user", password=USER_PASSWORD, is_staff=True)
        self.client.login(username=self.user.username, password=USER_PASSWORD)

    def mock_call_course_api(self, method, url, payload):
        """
        Helper method to make api calls using test client.
        """
        response = None
        if method == 'POST':
            response = self.client.post(
                url,
                data=payload,
                format='json'
            )
        elif method == 'PATCH':
            response = self.client.patch(
                url,
                data=payload,
                format='json'
            )
        return response

    def _assert_default_logs(self, log_capture):
        """
        Assert the initiation and completion logs are present in the logger.
        """
        log_capture.check_present(
            (
                LOGGER_PATH,
                'INFO',
                'Initiating CSV data loader flow.'
            ),
            (
                LOGGER_PATH,
                'INFO',
                'CSV loader ingest pipeline has completed.'
            )

        )

    def test_missing_organization(self, jwt_decode_patch):  # pylint: disable=unused-argument
        """
        Verify that no course and course run are created for a missing organization in the database.
        """
        with NamedTemporaryFile() as csv:
            csv = self._write_csv(csv, [mock_data.INVALID_ORGANIZATION_DATA])
            with LogCapture(LOGGER_PATH) as log_capture:
                with LogCapture(MIXIN_LOGGER_PATH) as log_capture_mixin:
                    loader = CSVDataLoader(self.partner, csv_path=csv.name, product_source=self.source.slug)
                    loader.ingest()
                    self._assert_default_logs(log_capture)
                    log_capture_mixin.check_present(
                        (
                            MIXIN_LOGGER_PATH,
                            'ERROR',
                            # pylint: disable=line-too-long
                            '[MISSING_ORGANIZATION] Unable to locate partner organization with key invalid-organization '
                            'for the course titled CSV Course.'
                        )
                    )
                    assert Course.objects.count() == 0
                    assert CourseRun.objects.count() == 0

    def test_invalid_course_type(self, jwt_decode_patch):  # pylint: disable=unused-argument
        """
        Verify that no course and course run are created for an invalid course track type.
        """
        self._setup_organization(self.partner)
        with NamedTemporaryFile() as csv:
            csv = self._write_csv(csv, [mock_data.INVALID_COURSE_TYPE_DATA])
            with LogCapture(LOGGER_PATH) as log_capture:
                with LogCapture(MIXIN_LOGGER_PATH) as log_capture_mixin:
                    loader = CSVDataLoader(self.partner, csv_path=csv.name, product_source=self.source.slug)
                    loader.ingest()
                    self._assert_default_logs(log_capture)
                    log_capture_mixin.check_present(
                        (
                            MIXIN_LOGGER_PATH,
                            'ERROR',
                            '[MISSING_COURSE_TYPE] Unable to find the course enrollment track "invalid track"'
                            ' for the course CSV Course'
                        )
                    )
                    assert Course.objects.count() == 0
                    assert CourseRun.objects.count() == 0

    def test_invalid_course_run_type(self, jwt_decode_patch):  # pylint: disable=unused-argument
        """
        Verify that no course and course run are created for an invalid course run track type.
        """
        self._setup_organization(self.partner)
        with NamedTemporaryFile() as csv:
            csv = self._write_csv(csv, [mock_data.INVALID_COURSE_RUN_TYPE_DATA])
            with LogCapture(LOGGER_PATH) as log_capture:
                with LogCapture(MIXIN_LOGGER_PATH) as log_capture_mixin:
                    loader = CSVDataLoader(self.partner, csv_path=csv.name, product_source=self.source.slug)
                    loader.ingest()
                    self._assert_default_logs(log_capture)
                    log_capture_mixin.check_present(
                        (
                            MIXIN_LOGGER_PATH,
                            'ERROR',
                            '[MISSING_COURSE_RUN_TYPE] Unable to find the course run enrollment track "invalid track"'
                            ' for the course CSV Course'
                        )
                    )
                    assert Course.objects.count() == 0
                    assert CourseRun.objects.count() == 0

    @responses.activate
    def test_image_download_failure(self, jwt_decode_patch):  # pylint: disable=unused-argument
        """
        Verify that if the course image download fails, the ingestion does not complete.
        """
        self._setup_prerequisites(self.partner)
        self.mock_studio_calls(self.partner)
        self.mock_ecommerce_publication(self.partner)
        responses.add(
            responses.GET,
            'https://example.com/image.jpg',
            status=400,
            body='Image unavailable',
            content_type='image/jpeg',
        )

        with NamedTemporaryFile() as csv:
            csv = self._write_csv(csv, [mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT])

            with LogCapture(LOGGER_PATH) as log_capture:
                with LogCapture(MIXIN_LOGGER_PATH) as log_capture_mixin:
                    with mock.patch.object(
                            CSVDataLoader,
                            'call_course_api',
                            self.mock_call_course_api
                    ):
                        loader = CSVDataLoader(self.partner, csv_path=csv.name, product_source=self.source.slug)
                        loader.ingest()

                        self._assert_default_logs(log_capture)
                        log_capture.check_present(
                            (
                                LOGGER_PATH,
                                'INFO',
                                'Course key edx+csv_123 could not be found in database, creating the course.'
                            )
                        )

                        # Creation call results in creating course and course run objects
                        self.assertEqual(Course.everything.count(), 1)
                        self.assertEqual(CourseRun.everything.count(), 1)

                        log_capture_mixin.check_present(
                            (
                                MIXIN_LOGGER_PATH,
                                'ERROR',
                                '[IMAGE_DOWNLOAD_FAILURE] The course image download failed for the course CSV Course.'
                            )
                        )

    @data(
        ('csv-course-custom-slug', 'executive-education/edx-csv-course', True),
        ('custom-slug-2', 'executive-education/edx-csv-course', False),
        ('', 'executive-education/edx-csv-course', True)
    )
    @unpack
    @responses.activate
    def test_single_valid_row(self, csv_slug, expected_slug, is_future_variant, jwt_decode_patch):  # pylint: disable=unused-argument
        """
        Verify that for a single row of valid data for a non-existent course, the draft and non-draft
        entries are created.
        """
        self._setup_prerequisites(self.partner)
        self.mock_studio_calls(self.partner)
        self.mock_ecommerce_publication(self.partner)
        _, image_content = self.mock_image_response()

        csv_data = {
            **mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT,
            'slug': csv_slug,
            'is_future_variant': is_future_variant
        }

        with NamedTemporaryFile() as csv:
            csv = self._write_csv(csv, [csv_data], headers=[*self.CSV_DATA_KEYS_ORDER, 'is_future_variant'])

            with LogCapture(LOGGER_PATH) as log_capture:
                with mock.patch.object(
                        CSVDataLoader,
                        'call_course_api',
                        self.mock_call_course_api
                ):
                    loader = CSVDataLoader(
                        self.partner, csv_path=csv.name,
                        product_type=self.course_type.slug,
                        product_source=self.source.slug
                    )

                    with mock.patch(
                        'course_discovery.apps.course_metadata.emails.send_email_for_legal_review'
                    ) as mocked_legal_email:
                        with override_waffle_switch(IS_SUBDIRECTORY_SLUG_FORMAT_ENABLED, active=True):
                            with override_waffle_switch(IS_SUBDIRECTORY_SLUG_FORMAT_FOR_EXEC_ED_ENABLED, active=True):
                                loader.ingest()
                    assert not mocked_legal_email.called

                    self._assert_default_logs(log_capture)
                    log_capture.check_present(
                        (
                            LOGGER_PATH,
                            'INFO',
                            'Course key edx+csv_123 could not be found in database, creating the course.'
                        )
                    )

                    for model in [Course, CourseRun, Seat, CourseEntitlement]:
                        assert model.objects.count() == 1
                        assert model.everything.count() == 2

                    course = Course.everything.get(key=self.COURSE_KEY, partner=self.partner, draft=True)
                    course_run = CourseRun.everything.get(course=course, draft=True)

                    official_course = course.official_version
                    official_course_run = course_run.official_version

                    assert course.image.read() == image_content
                    assert course.organization_logo_override.read() == image_content
                    self._assert_course_data(course, self.BASE_EXPECTED_COURSE_DATA)
                    self._assert_course_run_data(course_run, self.BASE_EXPECTED_COURSE_RUN_DATA)

                    self._assert_course_data(
                        official_course, {**self.BASE_EXPECTED_COURSE_DATA, 'draft': False}
                    )
                    self._assert_course_run_data(
                        official_course_run, {**self.BASE_EXPECTED_COURSE_RUN_DATA, 'draft': False}
                    )

                    assert course.entitlements.get().official_version == official_course.entitlements.get()
                    assert course_run.seats.get().official_version == official_course_run.seats.get()
                    assert course.active_url_slug == expected_slug
                    assert course.official_version.active_url_slug == expected_slug

                    assert TaxiForm.objects.count() == 1

                    assert loader.get_ingestion_stats() == {
                        'total_products_count': 1,
                        'success_count': 1,
                        'failure_count': 0,
                        'updated_products_count': 0,
                        'created_products_count': 1,
                        'created_products': [{
                            'uuid': str(course.uuid),
                            'external_course_marketing_type': 'short_course',
                            'url_slug': expected_slug,
                            'rerun': True,
                            'course_run_variant_id': str(course.course_runs.last().variant_id),
                            'is_future_variant': is_future_variant,
                            'restriction_type': None,
                        }],
                        'archived_products_count': 0,
                        'archived_products': [],
                        'errors': loader.error_logs
                    }

    @responses.activate
    def test_archived_flow_published_course(self, jwt_decode_patch):  # pylint: disable=unused-argument
        """
        Verify that the loader archives courses not in input data against the provided product source only.
        """
        self._setup_prerequisites(self.partner)
        self.mock_studio_calls(self.partner)
        self.mock_ecommerce_publication(self.partner)
        self.mock_image_response()

        additional_metadata_one = AdditionalMetadataFactory(product_status=ExternalProductStatus.Published)
        CourseFactory(
            key='test+123', partner=self.partner, type=self.course_type,
            draft=False, additional_metadata=additional_metadata_one,
            product_source=self.source
        )

        additional_metadata_two = AdditionalMetadataFactory(product_status=ExternalProductStatus.Published)
        CourseFactory(
            key='test+124', partner=self.partner, type=self.course_type,
            draft=False, additional_metadata=additional_metadata_two,
            product_source=self.source
        )

        additional_metadata__source_2 = AdditionalMetadataFactory(product_status=ExternalProductStatus.Published)
        CourseFactory(
            key='test+125', partner=self.partner, type=self.course_type,
            draft=False, additional_metadata=additional_metadata_two,
            product_source=SourceFactory(slug='ext_source_2')
        )

        with NamedTemporaryFile() as csv:
            csv = self._write_csv(csv, [mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT])

            with LogCapture(LOGGER_PATH) as log_capture:
                with mock.patch.object(
                        CSVDataLoader,
                        'call_course_api',
                        self.mock_call_course_api
                ):
                    loader = CSVDataLoader(
                        self.partner,
                        csv_path=csv.name,
                        product_type=CourseType.EXECUTIVE_EDUCATION_2U,
                        product_source=self.source.slug
                    )
                    loader.ingest()

                    self._assert_default_logs(log_capture)
                    log_capture.check_present(
                        (
                            LOGGER_PATH,
                            'INFO',
                            f'Archived 2 products in CSV Ingestion for source {self.source.slug} and product type '
                            f'{CourseType.EXECUTIVE_EDUCATION_2U}.'
                        ),
                    )

                    # Verify the existence of both draft and non-draft versions
                    assert Course.everything.count() == 5
                    assert AdditionalMetadata.objects.count() == 4

                    course = Course.everything.get(key=self.COURSE_KEY, draft=True)
                    stats = loader.get_ingestion_stats()
                    archived_products = stats.pop('archived_products')
                    assert stats == {
                        'total_products_count': 1,
                        'success_count': 1,
                        'failure_count': 0,
                        'updated_products_count': 0,
                        'created_products_count': 1,
                        'created_products': [{
                            'uuid': str(course.uuid),
                            'external_course_marketing_type': 'short_course',
                            'url_slug': 'csv-course',
                            'rerun': True,
                            'course_run_variant_id': str(course.course_runs.last().variant_id),
                            'restriction_type': None,
                            'is_future_variant': False,
                        }],
                        'archived_products_count': 2,
                        'errors': loader.error_logs
                    }

                    # asserting separately due to random sort order
                    assert set(archived_products) == {additional_metadata_one.external_identifier,
                                                      additional_metadata_two.external_identifier}

                    # Assert that a product status with different product source is not affected in Archive flow.
                    additional_metadata__source_2.refresh_from_db()
                    assert additional_metadata__source_2.product_status == ExternalProductStatus.Published

    @responses.activate
    def test_ingest_flow_for_preexisting_published_course(self, jwt_decode_patch):  # pylint: disable=unused-argument
        """
        Verify that the loader uses False draft flag for a published course run.
        """
        self._setup_prerequisites(self.partner)
        self.mock_studio_calls(self.partner)
        self.mock_ecommerce_publication(self.partner)
        self.mock_image_response()

        course = CourseFactory(key=self.COURSE_KEY, partner=self.partner, type=self.course_type, draft=True)
        CourseRunFactory(
            course=course,
            key=self.COURSE_RUN_KEY,
            type=self.course_run_type,
            status='published',
            draft=True,
            variant_id='00000000-0000-0000-0000-000000000000'
        )
        expected_course_data = {
            **self.BASE_EXPECTED_COURSE_DATA,
            'draft': False,
        }
        expected_course_run_data = {
            **self.BASE_EXPECTED_COURSE_RUN_DATA,
            'draft': False,
            'status': 'published'
        }

        with NamedTemporaryFile() as csv:
            csv = self._write_csv(csv, [mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT])

            with LogCapture(LOGGER_PATH) as log_capture:
                with mock.patch.object(
                        CSVDataLoader,
                        'call_course_api',
                        self.mock_call_course_api
                ):
                    loader = CSVDataLoader(self.partner, csv_path=csv.name, product_source=self.source.slug)
                    loader.ingest()

                    self._assert_default_logs(log_capture)
                    log_capture.check_present(
                        (
                            LOGGER_PATH,
                            'INFO',
                            'Course edx+csv_123 is located in the database.'
                        ),
                        (
                            LOGGER_PATH,
                            'INFO',
                            'Draft flag is set to False for the course CSV Course'
                        )
                    )

                    # Verify the existence of both draft and non-draft versions
                    assert Course.everything.count() == 2
                    assert CourseRun.everything.count() == 2

                    course = Course.objects.get(key=self.COURSE_KEY, partner=self.partner)
                    course_run = CourseRun.objects.get(course=course)

                    self._assert_course_data(course, expected_course_data)
                    self._assert_course_run_data(course_run, expected_course_run_data)

                    assert course.product_source == self.source
                    assert course.draft_version.product_source == self.source

                    assert loader.get_ingestion_stats() == {
                        'total_products_count': 1,
                        'success_count': 1,
                        'failure_count': 0,
                        'updated_products_count': 1,
                        'created_products_count': 0,
                        'created_products': [],
                        'archived_products_count': 0,
                        'archived_products': [],
                        'errors': loader.error_logs
                    }

    @responses.activate
    def test_ingest_flow_for_preexisting_published_course_with_new_run_creation(self, jwt_decode_patch):  # pylint: disable=unused-argument
        """
        Verify that the loader makes a new course run for a published course run if none of variant_id or start and end
        dates match the existing course run.
        """
        self._setup_prerequisites(self.partner)
        self.mock_studio_calls(self.partner)
        studio_url = '{root}/api/v1/course_runs/'.format(root=self.partner.studio_url.strip('/'))
        responses.add(responses.POST, f'{studio_url}{self.COURSE_RUN_KEY}/rerun/', status=200)
        self.mock_studio_calls(self.partner, run_key='course-v1:edx+csv_123+1T2020a')
        self.mock_ecommerce_publication(self.partner)
        self.mock_image_response()

        course = CourseFactory(
            key=self.COURSE_KEY,
            partner=self.partner,
            type=self.course_type,
            draft=True,
            key_for_reruns=''
        )
        CourseRunFactory(
            course=course,
            start=datetime.datetime(2014, 3, 1, tzinfo=UTC),
            # 2050 end date is to ensure the course run is present among active runs and thus
            # non-draft entries are created. If the discovery is there till 2050, you would need to update the
            # tests after Jan 1, 2050.
            end=datetime.datetime(2050, 1, 1, tzinfo=UTC),
            key=self.COURSE_RUN_KEY,
            type=self.course_run_type,
            status='published',
            draft=True,
        )
        expected_course_data = {
            **self.BASE_EXPECTED_COURSE_DATA,
        }
        expected_course_run_data = {
            **self.BASE_EXPECTED_COURSE_RUN_DATA,
        }

        assert Seat.everything.count() == 0

        with NamedTemporaryFile() as csv:
            csv = self._write_csv(csv, [mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT])
            with override_waffle_switch(IS_COURSE_RUN_VARIANT_ID_EDITABLE, active=True):
                with LogCapture(LOGGER_PATH) as log_capture:
                    with mock.patch.object(
                            CSVDataLoader,
                            'call_course_api',
                            self.mock_call_course_api
                    ):
                        loader = CSVDataLoader(self.partner, csv_path=csv.name, product_source=self.source.slug)
                        loader.ingest()

                        self._assert_default_logs(log_capture)
                        log_capture.check_present(
                            (
                                LOGGER_PATH,
                                'INFO',
                                'Course edx+csv_123 is located in the database.'
                            ),
                            (
                                LOGGER_PATH,
                                'INFO',
                                (
                                    'Course Run with variant_id {variant_id} could not be found.' +
                                    'Creating new course run for course {course_key} with variant_id {variant_id}'
                                ).format(
                                    variant_id='00000000-0000-0000-0000-000000000000',
                                    course_key=self.COURSE_KEY,
                                )
                            ),
                            (
                                LOGGER_PATH,
                                'INFO',
                                'Draft flag is set to False for the course CSV Course'
                            ),
                        )

                        # Verify the existence of both draft and non-draft versions
                        assert Course.everything.count() == 2
                        # Total course_runs count is 4 -> 2 for existing course runs (draft/non-draft)
                        # and 2 for new course run (draft/non-draft)
                        assert CourseRun.everything.count() == 4

                        assert Seat.everything.count() == 2
                        assert CourseEntitlement.everything.count() == 2

                        course = Course.objects.filter_drafts(key=self.COURSE_KEY, partner=self.partner).first()
                        course_run = CourseRun.everything.get(
                            course=course,
                            variant_id='00000000-0000-0000-0000-000000000000'
                        )

                        self._assert_course_data(course, expected_course_data)
                        self._assert_course_data(course.official_version, {**expected_course_data, 'draft': False})
                        self._assert_course_run_data(course_run, expected_course_run_data)
                        self._assert_course_run_data(
                            course_run.official_version, {**expected_course_run_data, 'draft': False}
                        )

                        assert course.product_source == self.source
                        assert course.official_version.product_source == self.source

                        assert loader.get_ingestion_stats() == {
                            'total_products_count': 1,
                            'success_count': 1,
                            'failure_count': 0,
                            'updated_products_count': 0,
                            'created_products_count': 1,
                            'created_products': [{
                                'uuid': str(course.uuid),
                                'external_course_marketing_type':
                                    course.additional_metadata.external_course_marketing_type,
                                'url_slug': course.active_url_slug,
                                'rerun': True,
                                'course_run_variant_id': str(course_run.variant_id),
                                'restriction_type': None,
                                'is_future_variant': False,
                            }],
                            'archived_products_count': 0,
                            'archived_products': [],
                            'errors': loader.error_logs
                        }

    @responses.activate
    def test_success_flow_course_with_multiple_variants(self, jwt_decode_patch):  # pylint: disable=unused-argument
        """
        Verify that the loader correctly ingests multiple variants of a course.
        """
        self._setup_prerequisites(self.partner)
        self.mock_studio_calls(self.partner)
        studio_url = '{root}/api/v1/course_runs/'.format(root=self.partner.studio_url.strip('/'))
        responses.add(responses.POST, f'{studio_url}{self.COURSE_RUN_KEY}/rerun/', status=200)
        self.mock_studio_calls(self.partner, run_key='course-v1:edx+csv_123+1T2020a')
        self.mock_ecommerce_publication(self.partner)
        _, _ = self.mock_image_response()

        course = CourseFactory(
            key=self.COURSE_KEY,
            partner=self.partner,
            type=self.course_type,
            draft=True,
            key_for_reruns=''
        )

        CourseRunFactory(
            course=course,
            start=datetime.datetime(2014, 3, 1, tzinfo=UTC),
            end=datetime.datetime(2040, 3, 1, tzinfo=UTC),
            key=self.COURSE_RUN_KEY,
            type=self.course_run_type,
            status='published',
            draft=True,
        )

        mocked_data = copy.deepcopy(mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT)
        mocked_data.update(
            {
                "publish_date": "01/26/2022",
                "start_date": "01/25/2022",
                "start_time": "00:00",
                "end_date": "02/25/2055",
                "end_time": "00:00",
                "reg_close_date": "01/25/2055",
                "reg_close_time": "00:00",
                "variant_id": "11111111-1111-1111-1111-111111111111",
            }
        )

        with NamedTemporaryFile() as csv:
            csv = self._write_csv(csv, [mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT, mocked_data])
            with override_waffle_switch(IS_COURSE_RUN_VARIANT_ID_EDITABLE, active=True):
                with LogCapture(LOGGER_PATH):
                    with mock.patch.object(
                            CSVDataLoader,
                            'call_course_api',
                            self.mock_call_course_api
                    ):
                        loader = CSVDataLoader(self.partner, csv_path=csv.name, product_source=self.source.slug)
                        loader.ingest()

                assert Course.everything.count() == 2
                assert CourseRun.everything.count() == 4

    @responses.activate
    def test_exception_flow_for_update_course(self, jwt_decode_patch):  # pylint: disable=unused-argument
        """
        Verify that the course update fails if an exception is raised while updating the course.
        """
        self._setup_prerequisites(self.partner)
        self.mock_studio_calls(self.partner)
        _ = self.mock_image_response()

        course = CourseFactory(key=self.COURSE_KEY, partner=self.partner, type=self.course_type, draft=True)
        CourseRunFactory(
            course=course,
            key=self.COURSE_RUN_KEY,
            type=self.course_run_type,
            status='published',
            draft=True,
            variant_id='00000000-0000-0000-0000-000000000000'
        )

        with NamedTemporaryFile() as csv:
            csv = self._write_csv(csv, [mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT])

            with mock.patch.object(
                CSVDataLoader, "call_course_api", self.mock_call_course_api
            ):
                loader = CSVDataLoader(
                    self.partner, csv_path=csv.name, product_source=self.source.slug
                )

                loader.register_ingestion_error = mock.MagicMock()
                loader.update_course = mock.MagicMock()

                loader.update_course.side_effect = MockExceptionWithResponse(b"Update course error")

                with LogCapture(LOGGER_PATH):
                    loader.ingest()

                    expected_error_message = CSVIngestionErrorMessages.COURSE_UPDATE_ERROR.format(
                        course_title=mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT["title"],
                        exception_message="Update course error",
                    )
                    loader.register_ingestion_error.assert_called_once_with(
                        CSVIngestionErrors.COURSE_UPDATE_ERROR, expected_error_message
                    )
                    self.assertEqual(Course.everything.count(), 1)
                    self.assertEqual(CourseRun.everything.count(), 1)

    @responses.activate
    def test_exception_flow_for_update_course_entitlement_price(self, jwt_decode_patch):  # pylint: disable=unused-argument
        """
        Verify that the course update fails if an exception is raised while updating the course.
        """
        self._setup_prerequisites(self.partner)
        self.mock_studio_calls(self.partner)
        studio_url = '{root}/api/v1/course_runs/'.format(root=self.partner.studio_url.strip('/'))
        responses.add(responses.POST, f'{studio_url}{self.COURSE_RUN_KEY}/rerun/', status=200)
        self.mock_studio_calls(self.partner, run_key='course-v1:edx+csv_123+1T2020a')
        self.mock_ecommerce_publication(self.partner)
        _, _ = self.mock_image_response()

        course = CourseFactory(
            key=self.COURSE_KEY,
            partner=self.partner,
            type=self.course_type,
            draft=True,
            key_for_reruns=''
        )

        CourseRunFactory(
            course=course,
            start=datetime.datetime(2014, 3, 1, tzinfo=UTC),
            end=datetime.datetime(2040, 3, 1, tzinfo=UTC),
            key=self.COURSE_RUN_KEY,
            type=self.course_run_type,
            status='published',
            draft=True,
        )

        mocked_data = copy.deepcopy(mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT)
        mocked_data.update(
            {
                "publish_date": "01/26/2022",
                "start_date": "01/25/2022",
                "start_time": "00:00",
                "end_date": "02/25/2055",
                "end_time": "00:00",
                "reg_close_date": "01/25/2055",
                "reg_close_time": "00:00",
                "variant_id": "11111111-1111-1111-1111-111111111111",
            }
        )

        with NamedTemporaryFile() as csv:
            csv = self._write_csv(csv, [mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT, mocked_data])
            with override_waffle_switch(IS_COURSE_RUN_VARIANT_ID_EDITABLE, active=True):
                with mock.patch.object(
                    CSVDataLoader, "call_course_api", self.mock_call_course_api
                ):
                    loader = CSVDataLoader(
                        self.partner, csv_path=csv.name, product_source=self.source.slug
                    )
                    loader.register_ingestion_error = mock.MagicMock()
                    # pylint: disable=protected-access
                    loader._update_course_entitlement_price = mock.MagicMock()

                    # pylint: disable=protected-access
                    loader._update_course_entitlement_price.side_effect = (
                        MockExceptionWithResponse('Entitlement Price Update Error')
                    )

                    with LogCapture(LOGGER_PATH):
                        loader.ingest()

                        expected_error_message = CSVIngestionErrorMessages.COURSE_ENTITLEMENT_PRICE_UPDATE_ERROR.format(
                            course_title=mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT["title"],
                            exception_message="Entitlement Price Update Error",
                        )
                        loader.register_ingestion_error.assert_called_once_with(
                            CSVIngestionErrors.COURSE_UPDATE_ERROR, expected_error_message
                        )

    @responses.activate
    @mock.patch("course_discovery.apps.course_metadata.data_loaders.csv_loader.reverse")
    @mock.patch("course_discovery.apps.course_metadata.data_loaders.csv_loader.settings")
    def test_update_course_entitlement_price_failure(
        self, mock_settings, mock_reverse, jwt_decode_patch
    ):  # pylint: disable=unused-argument
        """
        Verify that the course entitlement price update fails if an exception is raised while updating the course.
        """
        self._setup_prerequisites(self.partner)
        self.mock_studio_calls(self.partner)
        course = CourseFactory(
            key=self.COURSE_KEY, partner=self.partner, type=self.course_type, draft=True
        )
        CourseRunFactory(
            course=course,
            key=self.COURSE_RUN_KEY,
            type=self.course_run_type,
            status="published",
            draft=True,
            variant_id="00000000-0000-0000-0000-000000000000",
        )
        mock_settings.DISCOVERY_BASE_URL = "http://localhost:18381"
        mock_reverse.return_value = f"/api/v1/course/{course.uuid}"

        entitlement_price_url = f"http://localhost:18381/api/v1/course/{course.uuid}"
        responses.add(
            responses.PATCH,
            entitlement_price_url,
            json={"detail": "Error occurred"},
            status=204,
        )

        req_data = {
            "verified_price": "200",
            "restriction_type": "None",
            "title": "Test Course",
        }
        course_uuid = course.uuid
        course_type = CourseTypeFactory(slug=CourseType.EXECUTIVE_EDUCATION_2U)

        with NamedTemporaryFile() as csv:
            csv = self._write_csv(csv, [mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT])

            with LogCapture(LOGGER_PATH) as log_capture:
                loader = CSVDataLoader(self.partner, product_source=self.source.slug, csv_path=csv.name)
                # pylint: disable=protected-access
                response = loader._update_course_entitlement_price(req_data, course_uuid, course_type, is_draft=False)
                mock_reverse.assert_called_once_with("api:v1:course-detail", kwargs={"key": course_uuid})
                log_capture.check_present(
                    (
                        LOGGER_PATH,
                        "INFO",
                        "Entitlement price update response: b'{\"detail\": \"Error occurred\"}'",
                    ),
                )
                assert response == {"detail": "Error occurred"}

    @responses.activate
    @mock.patch("course_discovery.apps.course_metadata.data_loaders.csv_loader.download_and_save_course_image")
    def test_is_logo_downloaded_failure(self, mock_download_image, jwt_decode_patch):  # pylint: disable=unused-argument
        """
        Test case to verify that organization_logo_override image download failure is handled properly.
        """
        self._setup_prerequisites(self.partner)
        self.mock_studio_calls(self.partner)
        self.mock_ecommerce_publication(self.partner)
        _ = self.mock_image_response()
        course = CourseFactory(
            key=self.COURSE_KEY, partner=self.partner, type=self.course_type, draft=True
        )
        CourseRunFactory(
            course=course,
            key=self.COURSE_RUN_KEY,
            type=self.course_run_type,
            status="published",
            draft=True,
            variant_id="00000000-0000-0000-0000-000000000000",
        )

        def download_side_effect(*args, **kwargs):
            if 'organization_logo_override' in args:
                return False  # fail for organization_logo_override
            return True  # Succeed for course_image

        mock_download_image.side_effect = download_side_effect

        with NamedTemporaryFile() as csv:
            csv = self._write_csv(csv, [mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT])

            with mock.patch.object(
                CSVDataLoader,
                'call_course_api',
                self.mock_call_course_api
            ):
                with mock.patch.object(CSVDataLoader, 'register_ingestion_error') as mock_register_error:
                    loader = CSVDataLoader(self.partner, product_source=self.source.slug, csv_path=csv.name)
                    loader.ingest()

                    expected_error_message = CSVIngestionErrorMessages.LOGO_IMAGE_DOWNLOAD_FAILURE.format(
                        course_title=mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT['title']
                    )
                    mock_register_error.assert_any_call(
                        CSVIngestionErrors.LOGO_IMAGE_DOWNLOAD_FAILURE, expected_error_message
                    )

    @responses.activate
    def test_invalid_language(self, jwt_decode_patch):  # pylint: disable=unused-argument
        """
        Verify that the course run update fails if an invalid language information is provided
        in the data but the course information is updated properly.
        """
        self._setup_prerequisites(self.partner)
        self.mock_studio_calls(self.partner)
        _, image_content = self.mock_image_response()

        with NamedTemporaryFile() as csv:
            csv = self._write_csv(csv, [mock_data.INVALID_LANGUAGE])

            with LogCapture(LOGGER_PATH) as log_capture:
                with LogCapture(MIXIN_LOGGER_PATH) as log_capture_mixin:
                    with mock.patch.object(
                            CSVDataLoader,
                            'call_course_api',
                            self.mock_call_course_api
                    ):
                        loader = CSVDataLoader(self.partner, csv_path=csv.name, product_source=self.source.slug)
                        loader.ingest()

                        self._assert_default_logs(log_capture)

                        log_capture.check_present(
                            (
                                LOGGER_PATH,
                                'INFO',
                                'Course key edx+csv_123 could not be found in database, creating the course.'
                            ),
                            (
                                LOGGER_PATH,
                                'INFO',
                                'Draft flag is set to True for the course CSV Course'
                            )
                        )
                        log_capture_mixin.check_present(
                            (
                                MIXIN_LOGGER_PATH,
                                'ERROR',
                                '[COURSE_RUN_UPDATE_ERROR] Unable to update course run of the course CSV Course '
                                'in the system. The update failed with the exception: '
                                'Language gibberish-language from provided string gibberish-language'
                                ' is either missing or an invalid ietf language'
                            )
                        )

                        self.assertEqual(Course.everything.count(), 1)
                        self.assertEqual(CourseRun.everything.count(), 1)

                        course = Course.everything.get(key=self.COURSE_KEY, partner=self.partner)

                        assert course.image.read() == image_content
                        assert course.organization_logo_override.read() == image_content
                        self._assert_course_data(course, self.BASE_EXPECTED_COURSE_DATA)

    @responses.activate
    def test_ingest_flow_for_preexisting_unpublished_course(self, jwt_decode_patch):  # pylint: disable=unused-argument
        """
        Verify that the course run will be published if csv loader updates data for an unpublished course.
        """
        self._setup_prerequisites(self.partner)
        self.mock_studio_calls(self.partner)
        self.mock_ecommerce_publication(self.partner)
        self.mock_image_response()

        course = CourseFactory(
            key=self.COURSE_KEY, partner=self.partner, type=self.course_type, draft=True,
            additional_metadata=AdditionalMetadataFactory(taxi_form=None)
        )
        CourseRunFactory(
            course=course,
            key=self.COURSE_RUN_KEY,
            type=self.course_run_type,
            status='unpublished',
            draft=True,
            fixed_price_usd=111.11
        )

        with NamedTemporaryFile() as csv:
            csv = self._write_csv(csv, [{
                **mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT,
                "fixed_price_usd": "",
                "taxi_form_id": ""
            }])
            with LogCapture(LOGGER_PATH) as log_capture:
                with mock.patch.object(
                        CSVDataLoader,
                        'call_course_api',
                        self.mock_call_course_api
                ):

                    loader = CSVDataLoader(self.partner, csv_path=csv.name, product_source=self.source.slug)
                    loader.ingest()

                    log_capture.check_present(
                        (
                            LOGGER_PATH,
                            'INFO',
                            'Course edx+csv_123 is located in the database.'
                        ),
                        (
                            LOGGER_PATH,
                            'INFO',
                            'Draft flag is set to True for the course CSV Course'
                        )
                    )

                    # Verify the existence of draft and non-draft
                    assert Course.everything.count() == 2
                    assert CourseRun.everything.count() == 2
                    assert TaxiForm.objects.count() == 0

                    course = Course.everything.get(key=self.COURSE_KEY, partner=self.partner, draft=True)
                    course_run = CourseRun.everything.get(course=course, draft=True)

                    self._assert_course_data(course, {**self.BASE_EXPECTED_COURSE_DATA, 'taxi_form_is_none': True})
                    self._assert_course_run_data(
                        course_run,
                        {**self.BASE_EXPECTED_COURSE_RUN_DATA, "fixed_price_usd": Decimal('111.11')}
                    )

    @responses.activate
    @data(CourseRunStatus.LegalReview, CourseRunStatus.InternalReview)
    def test_ingest_flow_for_preexisting_course_having_run_in_review_statuses(
        self, status, jwt_decode_patch
    ):  # pylint: disable=unused-argument
        """
        Verify that the course run will be reviewed if csv loader updates data for a course having a run in legal
        review status.
        """
        self._setup_prerequisites(self.partner)
        self.mock_studio_calls(self.partner)
        self.mock_ecommerce_publication(self.partner)
        self.mock_image_response()

        course = CourseFactory(
            key=self.COURSE_KEY, partner=self.partner, type=self.course_type, draft=True,
            additional_metadata=AdditionalMetadataFactory(taxi_form=None)
        )
        CourseRunFactory(
            course=course,
            key=self.COURSE_RUN_KEY,
            type=self.course_run_type,
            status=status,
            go_live_date=datetime.datetime.now(UTC) - datetime.timedelta(days=5),
            draft=True,
            fixed_price_usd=111.11
        )

        with NamedTemporaryFile() as csv:
            csv = self._write_csv(csv, [{
                **mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT,
            }])
            with LogCapture(LOGGER_PATH) as log_capture:
                with mock.patch.object(
                        CSVDataLoader,
                        'call_course_api',
                        self.mock_call_course_api
                ):

                    loader = CSVDataLoader(self.partner, csv_path=csv.name, product_source=self.source.slug)
                    loader.ingest()

                    log_capture.check_present(
                        (
                            LOGGER_PATH,
                            'INFO',
                            'Course edx+csv_123 is located in the database.'
                        ),
                        (
                            LOGGER_PATH,
                            'INFO',
                            'Draft flag is set to True for the course CSV Course'
                        )
                    )

                    # Verify the existence of draft and non-draft
                    assert Course.everything.count() == 2
                    assert CourseRun.everything.count() == 2

                course = Course.everything.get(key=self.COURSE_KEY, partner=self.partner, draft=True)
                course_run = CourseRun.everything.get(course=course, draft=True)

                self._assert_course_data(course, {**self.BASE_EXPECTED_COURSE_DATA})
                assert course_run.status == CourseRunStatus.Published

    @responses.activate
    def test_active_slug(self, jwt_decode_patch):  # pylint: disable=unused-argument
        """
        Verify that the correct slug is created for two courses with same title in different organizations.
        """
        test_org = OrganizationFactory(name='testOrg', key='testOrg', partner=self.partner)
        self._setup_prerequisites(self.partner)
        self.mock_ecommerce_publication(self.partner)
        self.mock_studio_calls(self.partner)
        self.mock_image_response()

        with NamedTemporaryFile() as csv:
            csv = self._write_csv(
                csv, [
                    mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT,
                    {**mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT, 'organization': test_org.key}
                ]
            )
            with LogCapture(LOGGER_PATH) as log_capture:
                with mock.patch.object(
                        CSVDataLoader,
                        'call_course_api',
                        self.mock_call_course_api
                ):
                    loader = CSVDataLoader(self.partner, csv_path=csv.name, product_source=self.source.slug)
                    loader.ingest()

                    self._assert_default_logs(log_capture)

                    log_capture.check_present(
                        (
                            LOGGER_PATH,
                            'INFO',
                            'Course key edx+csv_123 could not be found in database, creating the course.'
                        ),
                        (
                            LOGGER_PATH,
                            'INFO',
                            'Draft flag is set to True for the course CSV Course'
                        )
                    )

                    assert Course.everything.count() == 4
                    assert CourseRun.everything.count() == 4

                    course1 = Course.everything.get(key=self.COURSE_KEY, partner=self.partner, draft=True)
                    course2 = Course.everything.get(key='testOrg+csv_123', partner=self.partner, draft=True)

                    assert course1.active_url_slug == 'csv-course'
                    assert course2.active_url_slug == 'csv-course-2'

                    log_capture.check_present(
                        (
                            LOGGER_PATH,
                            'INFO',
                            '{}:CSV Course'.format(course1.uuid)
                        ),
                        (
                            LOGGER_PATH,
                            'INFO',
                            '{}:CSV Course'.format(course2.uuid)
                        )
                    )

    @responses.activate
    def test_ingest_flow_for_minimal_course_data(self, jwt_decode_patch):  # pylint: disable=unused-argument
        """
        Verify that the loader runs as expected for minimal set of data.
        """
        self._setup_prerequisites(self.partner)
        self.mock_ecommerce_publication(self.partner)
        self.mock_studio_calls(self.partner)
        self.mock_image_response()
        with NamedTemporaryFile() as csv:
            csv = self._write_csv(
                csv, [mock_data.VALID_MINIMAL_COURSE_AND_COURSE_RUN_CSV_DICT], self.MINIMAL_CSV_DATA_KEYS_ORDER
            )

            with LogCapture(LOGGER_PATH) as log_capture:
                with mock.patch.object(
                        CSVDataLoader,
                        'call_course_api',
                        self.mock_call_course_api
                ):
                    loader = CSVDataLoader(self.partner, csv_path=csv.name, product_source=self.source.slug)
                    loader.ingest()

                    self._assert_default_logs(log_capture)
                    log_capture.check_present(
                        (
                            LOGGER_PATH,
                            'INFO',
                            'Course key edx+csv_123 could not be found in database, creating the course.'
                        ),
                        (
                            LOGGER_PATH,
                            'INFO',
                            'Draft flag is set to True for the course CSV Course'
                        )
                    )

                    assert Course.everything.count() == 2
                    assert CourseRun.everything.count() == 2

                    course = Course.everything.get(key=self.COURSE_KEY, partner=self.partner, draft=True)
                    course_run = CourseRun.everything.get(course=course, draft=True)

                    # Asserting some required and optional values to verify the correctnesss
                    assert course.title == 'CSV Course'
                    assert course.short_description == '<p>Very short description</p>'
                    assert course.full_description == (
                        '<p>Organization,Title,Number,Course Enrollment track,Image,Short Description,Long Description,'
                        'Organization,Title,Number,Course Enrollment track,Image,'
                        'Short Description,Long Description,</p>'
                    )
                    assert course.syllabus_raw == '<p>Introduction to Algorithms</p>'
                    assert course.subjects.first().slug == "computer-science"
                    assert course_run.staff.exists() is False

    @data(True, False)
    @responses.activate
    def test_entitlement_price_update_for_custom_presentation(self, reverse_order, jwt_decode_patch):  # pylint: disable=unused-argument
        """
        Verify that the loader does not update price for custom-b2b-enterprise in Course's Entitlement.
        """
        self._setup_prerequisites(self.partner)
        self.mock_ecommerce_publication(self.partner)
        self.mock_studio_calls(self.partner)
        self.mock_image_response()
        csv_key_order = list(self.CSV_DATA_KEYS_ORDER)
        csv_key_order.append('restriction_type')

        csv_data = [
            {
                **mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT,
                "restriction_type": None,
                "short_description": "ABC",
            },
            {
                **mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT,
                "restriction_type": "custom-b2b-enterprise",
                "verified_price": "250",
                "variant_id": "11111111-1111-1111-1111-111111111111",
                "short_description": "ABC",
            },
        ]

        if reverse_order:
            csv_data = list(reversed(csv_data))

        with NamedTemporaryFile() as csv:
            csv = self._write_csv(csv, csv_data, csv_key_order)

            with LogCapture(LOGGER_PATH) as log_capture:
                with mock.patch.object(
                        CSVDataLoader,
                        'call_course_api',
                        self.mock_call_course_api
                ):
                    loader = CSVDataLoader(self.partner, csv_path=csv.name, product_source=self.source.slug)
                    loader.ingest()

                    self._assert_default_logs(log_capture)
                    course = Course.objects.get(title='CSV Course')
                    assert course.entitlements.count() == 1
                    assert course.entitlements.first().price == 150
                    assert course.short_description == '<p>ABC</p>'

    @responses.activate
    def test_ingest_product_metadata_flow_for_non_exec_ed(self, jwt_decode_patch):  # pylint: disable=unused-argument
        """
        Verify that the loader does not ingest product meta information for non-exec ed course type.
        """
        CourseTypeFactory(name='Bootcamp(2U)', slug='bootcamp-2u')
        csv_data = {
            **mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT,
            'course_enrollment_track': 'Bootcamp(2U)',  # Additional metadata can exist only for ExecEd and Bootcamp
        }
        self._setup_prerequisites(self.partner)
        self.mock_ecommerce_publication(self.partner)
        self.mock_studio_calls(self.partner)
        self.mock_image_response()
        with NamedTemporaryFile() as csv:
            csv = self._write_csv(csv, [csv_data], self.CSV_DATA_KEYS_ORDER)
            with LogCapture(LOGGER_PATH) as log_capture:
                with mock.patch.object(
                        CSVDataLoader,
                        'call_course_api',
                        self.mock_call_course_api
                ):
                    loader = CSVDataLoader(self.partner, csv_path=csv.name, product_source=self.source.slug)
                    loader.ingest()

                    self._assert_default_logs(log_capture)
                    log_capture.check_present(
                        (
                            LOGGER_PATH,
                            'INFO',
                            'Course key edx+csv_123 could not be found in database, creating the course.'
                        ),
                        (
                            LOGGER_PATH,
                            'INFO',
                            'Draft flag is set to True for the course CSV Course'
                        )
                    )

                    assert Course.everything.count() == 2
                    assert CourseRun.everything.count() == 2

                    course = Course.everything.get(key=self.COURSE_KEY, partner=self.partner, draft=True)

                    # Asserting some required and optional values to verify the correctness
                    assert course.title == 'CSV Course'
                    assert course.short_description == '<p>Very short description</p>'
                    assert course.full_description == (
                        '<p>Organization,Title,Number,Course Enrollment track,Image,Short Description,Long Description,'
                        'Organization,Title,Number,Course Enrollment track,Image,'
                        'Short Description,Long Description,</p>'
                    )
                    assert course.syllabus_raw == '<p>Introduction to Algorithms</p>'
                    assert course.subjects.first().slug == "computer-science"
                    assert course.additional_metadata.product_meta is None

    @data(
        (['certificate_header', 'certificate_text', 'stat1_text'],
         '[MISSING_REQUIRED_DATA] Course CSV Course is missing the required data for ingestion. '
         'The missing data elements are "certificate_header, certificate_text, stat1_text"',
         ExternalCourseMarketingType.ShortCourse.value
         ),
        (['certificate_header', 'certificate_text', 'stat1_text'],
         '[MISSING_REQUIRED_DATA] Course CSV Course is missing the required data for ingestion. '
         'The missing data elements are "stat1_text"',
         ExternalCourseMarketingType.Sprint.value
         ),
    )
    @unpack
    def test_data_validation__exec_education_external_marketing_types(
            self, missing_fields, expected_message, external_course_marketing_type, _jwt_decode_patch
    ):
        """
        Verify data validation of executive education course with different external course marketing types
        """
        self._setup_prerequisites(self.partner)
        course_type = ('Executive Education(2U)', 'executive-education-2u')
        product_source = 'ext_source'
        if not CourseType.objects.filter(name=course_type[0], slug=course_type[1]).exists():
            CourseTypeFactory(name=course_type[0], slug=course_type[1])

        if not Source.objects.filter(slug=product_source).exists():
            SourceFactory(slug=product_source)
        csv_data = {
            **mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT,
            'course_enrollment_track': course_type[0],
            'external_course_marketing_type': external_course_marketing_type
        }
        # Set data fields to be empty
        for field in missing_fields:
            csv_data[field] = ''

        with NamedTemporaryFile() as csv:
            csv = self._write_csv(csv, [csv_data])

            with LogCapture(LOGGER_PATH) as log_capture:
                with LogCapture(MIXIN_LOGGER_PATH) as log_capture_mixin:
                    loader = CSVDataLoader(
                        self.partner, csv_path=csv.name, product_type=course_type[1], product_source=product_source
                    )
                    loader.ingest()

                    self._assert_default_logs(log_capture)

                    log_capture_mixin.check_present(
                        (
                            MIXIN_LOGGER_PATH,
                            'ERROR',
                            expected_message
                        )
                    )

                    assert Course.everything.count() == 0
                    assert CourseRun.everything.count() == 0

    @data(
        (['primary_subject', 'image', 'long_description'],
         ('Executive Education(2U)', 'executive-education-2u'),
         'ext_source',
         '[MISSING_REQUIRED_DATA] Course CSV Course is missing the required data for ingestion. '
         'The missing data elements are "cardUrl, long_description, primary_subject"'
         ),
        (['publish_date', 'organic_url', 'stat1_text'],
         ('Executive Education(2U)', 'executive-education-2u'),
         'ext_source',
         '[MISSING_REQUIRED_DATA] Course CSV Course is missing the required data for ingestion. '
         'The missing data elements are "publish_date, organic_url, stat1_text"'
         ),
        (['organic_url', 'stat1_text', 'certificate_header'],
         ('Executive Education(2U)', 'executive-education-2u'),
         'dbz_source',
         '[MISSING_REQUIRED_DATA] Course CSV Course is missing the required data for ingestion. '
         'The missing data elements are "organic_url"'
         ),
        (['primary_subject', 'image', 'long_description'],
         ('Bootcamp(2U)', 'bootcamp-2u'),
         'ext_source',
         '[MISSING_REQUIRED_DATA] Course CSV Course is missing the required data for ingestion. '
         'The missing data elements are "cardUrl, long_description, primary_subject"'
         ),
        (['redirect_url', 'organic_url'],
         ('Bootcamp(2U)', 'bootcamp-2u'),
         'ext_source',
         '[MISSING_REQUIRED_DATA] Course CSV Course is missing the required data for ingestion. '
         'The missing data elements are "redirect_url, organic_url"'
         ),
        (['publish_date', 'organic_url', 'stat1_text'],  # ExEd data fields are not considered for other types
         ('Professional', 'prof-ed'),
         'ext_source',
         '[MISSING_REQUIRED_DATA] Course CSV Course is missing the required data for ingestion. '
         'The missing data elements are "publish_date"'
         ),
    )
    @unpack
    def test_data_validation_checks(
            self, missing_fields, course_type, product_source, expected_message, jwt_decode_patch
    ):  # pylint: disable=unused-argument
        """
        Verify that if any of the required field is missing in data, the ingestion is not done.
        """
        self._setup_prerequisites(self.partner)
        if not CourseType.objects.filter(name=course_type[0], slug=course_type[1]).exists():
            CourseTypeFactory(name=course_type[0], slug=course_type[1])

        if not Source.objects.filter(slug=product_source).exists():
            SourceFactory(slug=product_source)

        csv_data = {
            **mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT,
            'course_enrollment_track': course_type[0]
        }
        # Set data fields to be empty
        for field in missing_fields:
            csv_data[field] = ''

        with NamedTemporaryFile() as csv:
            csv = self._write_csv(csv, [csv_data])

            with LogCapture(LOGGER_PATH) as log_capture:
                with LogCapture(MIXIN_LOGGER_PATH) as log_capture_mixin:
                    loader = CSVDataLoader(
                        self.partner, csv_path=csv.name, product_type=course_type[1], product_source=product_source
                    )
                    loader.ingest()

                    self._assert_default_logs(log_capture)

                    log_capture_mixin.check_present(
                        (
                            MIXIN_LOGGER_PATH,
                            'ERROR',
                            expected_message
                        )
                    )

                    assert Course.everything.count() == 0
                    assert CourseRun.everything.count() == 0
