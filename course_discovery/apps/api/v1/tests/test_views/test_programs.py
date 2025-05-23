import datetime
import urllib.parse
from unittest import mock

import pytest
import pytz
from django.test import RequestFactory
from django.urls import reverse
from taggit.models import Tag

from course_discovery.apps.api.serializers import MinimalProgramSerializer
from course_discovery.apps.api.v1.tests.test_views.mixins import FuzzyInt, SerializationMixin
from course_discovery.apps.api.v1.views.programs import ProgramViewSet
from course_discovery.apps.core.tests.factories import USER_PASSWORD, UserFactory
from course_discovery.apps.core.tests.helpers import make_image_file
from course_discovery.apps.course_metadata.choices import CourseRunStatus, ProgramStatus
from course_discovery.apps.course_metadata.models import CourseType, Program, ProgramType
from course_discovery.apps.course_metadata.tests.factories import (
    CorporateEndorsementFactory, CourseFactory, CourseRunFactory, CurriculumCourseMembershipFactory, CurriculumFactory,
    CurriculumProgramMembershipFactory, DegreeAdditionalMetadataFactory, DegreeFactory, EndorsementFactory,
    ExpectedLearningItemFactory, JobOutlookItemFactory, OrganizationFactory, PersonFactory, ProgramFactory,
    ProgramTypeFactory, RestrictedCourseRunFactory, VideoFactory
)


@pytest.mark.django_db
@pytest.mark.usefixtures('django_cache')
class TestProgramViewSet(SerializationMixin):
    client = None
    django_assert_num_queries = None
    list_path = reverse('api:v1:program-list')
    partner = None
    request = None

    @pytest.fixture(autouse=True)
    def setup(self, client, django_assert_num_queries, partner):
        user = UserFactory(is_staff=True, is_superuser=True)

        client.login(username=user.username, password=USER_PASSWORD)

        site = partner.site
        request = RequestFactory(SERVER_NAME=site.domain).get('')
        request.site = site
        request.user = user

        self.client = client
        self.django_assert_num_queries = django_assert_num_queries
        self.partner = partner
        self.request = request

    def create_program(self, courses=None, program_type=None, include_restricted_run=False):
        organizations = [OrganizationFactory(partner=self.partner)]
        person = PersonFactory()

        if courses is None:
            courses = [CourseFactory(partner=self.partner)]
            course_run = CourseRunFactory(course=courses[0], staff=[person])

            if include_restricted_run:
                RestrictedCourseRunFactory(course_run=course_run, restriction_type='custom-b2c')

        if program_type is None:
            program_type = ProgramTypeFactory()

        topic, _ = Tag.objects.get_or_create(name="topic")

        program = ProgramFactory(
            courses=courses,
            authoring_organizations=organizations,
            credit_backing_organizations=organizations,
            corporate_endorsements=CorporateEndorsementFactory.create_batch(1),
            individual_endorsements=EndorsementFactory.create_batch(1),
            expected_learning_items=ExpectedLearningItemFactory.create_batch(1),
            job_outlook_items=JobOutlookItemFactory.create_batch(1),
            instructor_ordering=PersonFactory.create_batch(1),
            banner_image=make_image_file('test_banner.jpg'),
            video=VideoFactory(),
            partner=self.partner,
            type=program_type,
        )
        program.labels.add(topic)
        program.refresh_from_db()
        return program

    def create_curriculum(self, parent_program):
        person = PersonFactory()
        course = CourseFactory(partner=self.partner)
        CourseRunFactory(course=course, staff=[person])
        CourseRunFactory(course=course, staff=[person])

        curriculum = CurriculumFactory(
            program=parent_program
        )
        CurriculumCourseMembershipFactory(
            course=course,
            curriculum=curriculum
        )
        return curriculum

    def assert_retrieve_success(self, program, querystring=None):
        """ Verify the retrieve endpoint successfully returns a serialized program. """
        url = reverse('api:v1:program-detail', kwargs={'uuid': program.uuid})

        if querystring:
            url += '?' + urllib.parse.urlencode(querystring)

        response = self.client.get(url)
        assert response.status_code == 200
        return response

    def test_authentication(self):
        """ Verify the endpoint requires the user to be authenticated. """
        response = self.client.get(self.list_path)
        assert response.status_code == 200

        self.client.logout()
        response = self.client.get(self.list_path)
        assert response.status_code == 401

    def test_retrieve(self, django_assert_num_queries):
        """ Verify the endpoint returns the details for a single program. """
        program = self.create_program()

        with django_assert_num_queries(FuzzyInt(68, 3)):
            response = self.assert_retrieve_success(program)
        # property does not have the right values while being indexed
        del program._course_run_weeks_to_complete
        assert response.data == self.serialize_program(program)

        # Verify that requests including querystring parameters are cached separately.
        response = self.assert_retrieve_success(program, querystring={'use_full_course_serializer': 1})
        assert response.data == self.serialize_program(program, extra_context={'use_full_course_serializer': 1})

    def test_retrieve_basic_curriculum(self, django_assert_num_queries):
        program = self.create_program(courses=[])
        self.create_curriculum(program)
        program.refresh_from_db()
        with django_assert_num_queries(FuzzyInt(52, 3)):
            response = self.assert_retrieve_success(program)
        assert response.data == self.serialize_program(program)

    def test_retrieve_curriculum_with_child_programs(self, django_assert_num_queries):
        parent_program = self.create_program(courses=[])
        curriculum = self.create_curriculum(parent_program)

        child_program1 = self.create_program()
        child_program2 = self.create_program()
        CurriculumProgramMembershipFactory(
            program=child_program1,
            curriculum=curriculum
        )
        CurriculumProgramMembershipFactory(
            program=child_program2,
            curriculum=curriculum
        )
        parent_program.refresh_from_db()
        with django_assert_num_queries(FuzzyInt(85, 3)):
            response = self.assert_retrieve_success(parent_program)
        assert response.data == self.serialize_program(parent_program)

    @pytest.mark.parametrize('order_courses_by_start_date', (True, False,))
    def test_retrieve_with_sorting_flag(self, order_courses_by_start_date, django_assert_num_queries):
        """ Verify the number of queries is the same with sorting flag set to true. """
        course_list = CourseFactory.create_batch(3, partner=self.partner)
        for course in course_list:
            CourseRunFactory(course=course)
        program = ProgramFactory(
            courses=course_list,
            order_courses_by_start_date=order_courses_by_start_date,
            partner=self.partner)
        # property does not have the right values while being indexed
        del program._course_run_weeks_to_complete
        with django_assert_num_queries(FuzzyInt(51, 3)):
            response = self.assert_retrieve_success(program)
        assert response.data == self.serialize_program(program)
        assert course_list == list(program.courses.all())

    def test_retrieve_has_sorted_courses(self):
        """ Verify that runs inside a course are sorted properly. """
        course = CourseFactory(partner=self.partner)
        run1 = CourseRunFactory(course=course, start=datetime.datetime(2003, 1, 1, tzinfo=pytz.UTC))
        run2 = CourseRunFactory(course=course, start=datetime.datetime(2002, 1, 1, tzinfo=pytz.UTC))
        run3 = CourseRunFactory(course=course, start=datetime.datetime(2004, 1, 1, tzinfo=pytz.UTC))
        program = self.create_program(courses=[course])

        response = self.assert_retrieve_success(program)
        expected_keys = [run2.key, run1.key, run3.key]
        response_keys = [run['key'] for run in response.data['courses'][0]['course_runs']]
        assert expected_keys == response_keys

    def test_retrieve_without_course_runs(self, django_assert_num_queries):
        """ Verify the endpoint returns data for a program even if the program's courses have no course runs. """
        course = CourseFactory(partner=self.partner)
        program = ProgramFactory(courses=[course], partner=self.partner)
        with django_assert_num_queries(FuzzyInt(40, 2)):
            response = self.assert_retrieve_success(program)
        assert response.data == self.serialize_program(program)

    def assert_list_results(self, url, expected, expected_query_count, extra_context=None):
        """
        Asserts the results serialized/returned at the URL matches those that are expected.
        Args:
            url (str): URL from which data should be retrieved
            expected (list[Program]): Expected programs

        Notes:
            The API usually returns items in reverse order of creation (e.g. newest first). You may need to reverse
            the values of `expected` if you encounter issues. This method will NOT do that reversal for you.

        Returns:
            None
        """
        with self.django_assert_num_queries(FuzzyInt(expected_query_count, 2)):
            response = self.client.get(url)
        assert response.data['results'] == self.serialize_program(expected, many=True, extra_context=extra_context)

    def test_list(self):
        """ Verify the endpoint returns a list of all programs. """
        expected = [self.create_program() for __ in range(3)]

        self.assert_list_results(self.list_path, expected, 26)

    @pytest.mark.parametrize("include_restriction_param", [True, False])
    def test_list_restricted_runs(self, include_restriction_param):
        self.create_program(include_restricted_run=True)
        query_param_string = "?include_restricted=custom-b2c" if include_restriction_param else ""
        resp = self.client.get(self.list_path + query_param_string)

        if include_restriction_param:
            assert resp.data['results'][0]['courses'][0]['course_runs']
            assert resp.data['results'][0]['courses'][0]['course_run_statuses']
            assert resp.data['results'][0]['course_run_statuses'] == [CourseRunStatus.Published]
        else:
            assert not resp.data['results'][0]['courses'][0]['course_runs']
            assert not resp.data['results'][0]['courses'][0]['course_run_statuses']
            assert resp.data['results'][0]['course_run_statuses'] == []

    def test_extended_query_param_fields(self):
        """ Verify that the `extended` query param will result in an extended amount of fields returned. """
        for _ in range(3):
            self.create_program()

        extra_field_url = self.list_path + '?extended=True'
        extra_fields_program_set = self.client.get(extra_field_url)
        normal_list_program_set = self.client.get(self.list_path)
        for extended_program in extra_fields_program_set.data.get('results'):
            assert 'expected_learning_items' in extended_program.keys()
            assert 'price_ranges' in extended_program.keys()
        for minimal_program in normal_list_program_set.data.get('results'):
            assert 'expected_learning_items' not in minimal_program.keys()
            assert 'price_ranges' not in minimal_program.keys()

    def test_uuids_only(self):
        """
        Verify that the list view returns a simply list of UUIDs when the
        uuids_only query parameter is passed.
        """
        active = ProgramFactory.create_batch(3, partner=self.partner)
        retired = [ProgramFactory(status=ProgramStatus.Retired, partner=self.partner)]
        programs = active + retired

        querystring = {'uuids_only': 1}
        url = '{base}?{query}'.format(base=self.list_path, query=urllib.parse.urlencode(querystring))
        response = self.client.get(url)

        assert set(response.data) == {program.uuid for program in programs}

        # Verify that filtering (e.g., by status) is still supported.
        querystring['status'] = ProgramStatus.Retired
        url = '{base}?{query}'.format(base=self.list_path, query=urllib.parse.urlencode(querystring))
        response = self.client.get(url)

        assert set(response.data) == {program.uuid for program in retired}

    def test_filter_by_type(self):
        """ Verify that the endpoint filters programs to those of a given type. """
        program_type_name = 'foo'
        program = ProgramFactory(type__name_t=program_type_name, partner=self.partner)
        url = self.list_path + '?type=' + program_type_name
        self.assert_list_results(url, [program], 17)

        url = self.list_path + '?type=bar'
        self.assert_list_results(url, [], 5)

    def test_filter_by_types(self):
        """ Verify that the endpoint filters programs to those matching the provided ProgramType slugs. """
        expected = ProgramFactory.create_batch(2, partner=self.partner)
        type_slugs = [p.type.slug for p in expected]
        url = self.list_path + '?types=' + ','.join(type_slugs)

        # Create a third program, which should be filtered out.
        ProgramFactory(partner=self.partner)

        self.assert_list_results(url, expected, 18)

    def test_filter_by_timestamp(self):
        """
        Verify that the endpoint filters programs based on modified timestamp.
        """
        program1 = ProgramFactory(partner=self.partner)
        program2 = ProgramFactory(partner=self.partner)
        program3 = ProgramFactory(partner=self.partner)

        timestamp_now = datetime.datetime.now().isoformat()
        for programobj in [program1, program2, program3]:
            programobj.subtitle = 'test update'
            programobj.save()

        url = f"{self.list_path}?timestamp={timestamp_now}"
        response = self.client.get(url)
        assert response.status_code == 200
        assert len(response.data['results']) == 3

        # programs saved without modification do not show up in filtering
        timestamp_now = datetime.datetime.now().isoformat()
        for programobj in [program1, program2, program3]:
            programobj.save()

        url = f"{self.list_path}?timestamp={timestamp_now}"
        response = self.client.get(url)
        assert response.status_code == 200
        assert len(response.data['results']) == 0

    def test_filter_by_uuids(self):
        """ Verify that the endpoint filters programs to those matching the provided UUIDs. """
        expected = ProgramFactory.create_batch(2, partner=self.partner)
        uuids = [str(p.uuid) for p in expected]
        url = self.list_path + '?uuids=' + ','.join(uuids)

        # Create a third program, which should be filtered out.
        ProgramFactory(partner=self.partner)

        self.assert_list_results(url, expected, 18)

    @pytest.mark.parametrize(
        'status,is_marketable,expected_query_count',
        (
            (ProgramStatus.Unpublished, False, 5),
            (ProgramStatus.Active, True, 19),
        )
    )
    def test_filter_by_marketable(self, status, is_marketable, expected_query_count):
        """ Verify the endpoint filters programs to those that are marketable. """
        url = self.list_path + '?marketable=1'
        ProgramFactory(marketing_slug='', partner=self.partner)
        programs = ProgramFactory.create_batch(3, status=status, partner=self.partner)

        expected = programs if is_marketable else []
        assert list(Program.objects.marketable()) == expected
        self.assert_list_results(url, expected, expected_query_count)

    def test_filter_by_status(self):
        """ Verify the endpoint allows programs to filtered by one, or more, statuses. """
        active = ProgramFactory(status=ProgramStatus.Active, partner=self.partner)
        retired = ProgramFactory(status=ProgramStatus.Retired, partner=self.partner)

        url = self.list_path + '?status=active'
        self.assert_list_results(url, [active], 17)

        url = self.list_path + '?status=retired'
        self.assert_list_results(url, [retired], 17)

        url = self.list_path + '?status=active&status=retired'
        self.assert_list_results(url, [active, retired], 18)

    def test_filter_by_hidden(self):
        """ Endpoint should filter programs by their hidden attribute value. """
        hidden = ProgramFactory(hidden=True, partner=self.partner)
        not_hidden = ProgramFactory(hidden=False, partner=self.partner)

        url = self.list_path + '?hidden=True'
        self.assert_list_results(url, [hidden], 17)

        url = self.list_path + '?hidden=False'
        self.assert_list_results(url, [not_hidden], 17)

        url = self.list_path + '?hidden=1'
        self.assert_list_results(url, [hidden], 17)

        url = self.list_path + '?hidden=0'
        self.assert_list_results(url, [not_hidden], 17)

    def test_filter_by_marketing_slug(self):
        """ The endpoint should support filtering programs by marketing slug. """
        SLUG = 'test-program'

        # This program should not be included in the results below because it never matches the filter.
        self.create_program()

        url = f'{self.list_path}?marketing_slug={SLUG}'
        self.assert_list_results(url, [], 5)

        program = self.create_program()
        program.marketing_slug = SLUG
        program.save()

        self.assert_list_results(url, [program], 24)

    def test_list_exclude_utm(self):
        """ Verify the endpoint returns marketing URLs without UTM parameters. """
        url = self.list_path + '?exclude_utm=1'
        program = self.create_program()
        self.assert_list_results(url, [program], 23, extra_context={'exclude_utm': 1})

    def test_minimal_serializer_use(self):
        """ Verify that the list view uses the minimal serializer. """
        mock_request = mock.MagicMock()
        mock_request.query_params = dict()  # lint-amnesty, pylint: disable=use-dict-literal
        assert ProgramViewSet(action='list', request=mock_request).get_serializer_class() == MinimalProgramSerializer

    def test_update_card_image(self):
        program = self.create_program()
        image_dict = {
            'image': 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY'
                     '42YAAAAASUVORK5CYII=',
        }
        update_url = reverse('api:v1:program-update-card-image', kwargs={'uuid': program.uuid})
        response = self.client.post(update_url, image_dict, format='json')
        assert response.status_code == 200

    def test_update_card_image_authentication(self):
        program = self.create_program()
        self.client.logout()
        image_dict = {
            'image': 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY'
                     '42YAAAAASUVORK5CYII=',
        }
        update_url = reverse('api:v1:program-update-card-image', kwargs={'uuid': program.uuid})
        response = self.client.post(update_url, image_dict, format='json')
        assert response.status_code == 401

    def test_update_card_image_authentication_notstaff(self):
        program = self.create_program()
        self.client.logout()
        user = UserFactory(is_staff=False)
        self.client.login(username=user.username, password=USER_PASSWORD)
        image_dict = {
            'image': 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY'
                     '42YAAAAASUVORK5CYII=',
        }
        update_url = reverse('api:v1:program-update-card-image', kwargs={'uuid': program.uuid})
        response = self.client.post(update_url, image_dict, format='json')
        assert response.status_code == 403

    def test_update_card_malformed_image(self):
        program = self.create_program()
        image_dict = {
            'image': 'ARandomString',
        }
        update_url = reverse('api:v1:program-update-card-image', kwargs={'uuid': program.uuid})
        response = self.client.post(update_url, image_dict, format='json')
        assert response.status_code == 400

    def test_enterprise_subscription_inclusion(self):
        course_type = CourseType.objects.filter(slug=CourseType.VERIFIED_AUDIT).first()
        course = CourseFactory(enterprise_subscription_inclusion=True, type=course_type)
        course2 = CourseFactory(enterprise_subscription_inclusion=True, type=course_type)
        course3 = CourseFactory(enterprise_subscription_inclusion=False, type=course_type)
        course_list_false = [course, course2, course3]
        program_type = ProgramType.objects.get(translations__name_t='XSeries')
        program1 = self.create_program(courses=course_list_false, program_type=program_type)
        assert program1.enterprise_subscription_inclusion is False

        course_list_true = CourseFactory.create_batch(3, enterprise_subscription_inclusion=True, type=course_type)
        program2 = self.create_program(courses=course_list_true, program_type=program_type)
        assert program2.enterprise_subscription_inclusion is True

    def test_is_2u_degree_program(self):
        course_list = CourseFactory.create_batch(3)
        not_2u_degree_program = self.create_program(courses=course_list)
        assert not_2u_degree_program.is_2u_degree_program is False

        degree = DegreeFactory()
        DegreeAdditionalMetadataFactory(degree=degree)
        assert degree.is_2u_degree_program is True
