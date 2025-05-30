"""
Unit tests for populate_executive_education_data_csv management command.
"""
import copy
import csv
import json
from datetime import date
from tempfile import NamedTemporaryFile

import ddt
import mock
import responses
from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import TestCase
from testfixtures import LogCapture

from course_discovery.apps.course_metadata.data_loaders.tests import mock_data
from course_discovery.apps.course_metadata.data_loaders.tests.mixins import CSVLoaderMixin

LOGGER_PATH = 'course_discovery.apps.course_metadata.management.commands.populate_executive_education_data_csv'


@ddt.ddt
class TestPopulateExecutiveEducationDataCsv(CSVLoaderMixin, TestCase):
    """
    Test suite for populate_executive_education_data_csv management command.
    """
    AUTH_TOKEN = 'auth_token'
    SUCCESS_API_RESPONSE = {
        'products': [
            {
                "id": "12345678",
                "name": "CSV Course",
                "altName": "Alternative CSV Course",
                "abbreviation": "TC",
                "altAbbreviation": "UCT",
                "blurb": "A short description for CSV course",
                "language": "Español",
                "subjectMatter": "Marketing",
                "altSubjectMatter": "Design and Marketing",
                "altSubjectMatter1": "Marketing, Sales, and Techniques",
                "universityAbbreviation": "edX",
                "altUniversityAbbreviation": "altEdx",
                "cardUrl": "aHR0cHM6Ly9leGFtcGxlLmNvbS9pbWFnZS5qcGc=",
                "edxRedirectUrl": "aHR0cHM6Ly9leGFtcGxlLmNvbS8=",
                "edxPlpUrl": "aHR0cHM6Ly9leGFtcGxlLmNvbS8=",
                "durationWeeks": 10,
                "effort": "7–10 hours per week",
                'introduction': 'Very short description\n',
                'isThisCourseForYou': 'This is supposed to be a long description',
                'whatWillSetYouApart': "New ways to learn",
                "videoURL": "",
                "lcfURL": "d3d3LmV4YW1wbGUuY29tL2xlYWQtY2FwdHVyZT9pZD0xMjM=",
                "logoUrl": "aHR0cHM6Ly9leGFtcGxlLmNvbS9pbWFnZS5qcGc=g",
                "metaTitle": "SEO Title",
                "metaDescription": "SEO Description",
                "metaKeywords": "Keyword 1, Keyword 2",
                "slug": "csv-course-slug",
                "productType": "short_course",
                "prospectusUrl": "aHR0cHM6Ly93d3cuZ2V0c21hcnRlci5jb20vYmxvZy9jYXJlZXItYWR2aWNl",
                "edxTaxiFormId": "test-form-id",
                "variant": {
                    "id": "00000000-0000-0000-0000-000000000000",
                    "endDate": "2022-05-06",
                    "finalPrice": "1998",
                    "startDate": "2022-03-06",
                    "regCloseDate": "2022-02-06",
                    "finalRegCloseDate": "2022-02-15",
                    "enterprisePriceUsd": "333.3"
                },
                "curriculum": {
                    "heading": "Course curriculum",
                    "blurb": "Test Curriculum",
                    "modules": [
                        {
                            "module_number": 0,
                            "heading": "Module 0",
                            "description": "Welcome to your course"
                        },
                        {
                            "module_number": 1,
                            "heading": "Module 1",
                            "description": "Welcome to Module 1"
                        },
                    ]
                },
                "testimonials": [
                    {
                        "name": "Lorem Ipsum",
                        "title": "Gibberish",
                        "text": " This is a good course"
                    },
                ],
                "faqs": [
                    {
                        "id": "faq-1",
                        "headline": "FAQ 1",
                        "blurb": "This should answer it"
                    }
                ],
                "certificate": {
                    "headline": "About the certificate",
                    "blurb": "how this makes you special"
                },
                "stats": {
                    "stat1": "90%",
                    "stat1Blurb": "<p>A vast number of special beings take this course</p>",
                    "stat2": "100 million",
                    "stat2Blurb": "<p>VC fund</p>"
                }
            },
        ]}

    variant_1 = {
        "id": "00000000-0000-0000-0000-000000000000",
        "course": "Test Organisations Programme 2024-01-31",
        "currency": "USD",
        "normalPrice": 36991.0,
        "discount": 4000.0,
        "finalPrice": 32991.0,
        "regCloseDate": "2024-03-12",
        "startDate": "2024-03-20",
        "endDate": "2024-04-28",
        "finalRegCloseDate": "2024-03-26",
        "websiteVisibility": "private",
        "enterprisePriceUsd": 3510.0
    }

    variant_2 = {
        "id": "11111111-1111-1111-1111-111111111111",
        "course": "Test Organisations Programme 2024-02-06",
        "currency": "USD",
        "normalPrice": 36991.0,
        "discount": 4000.0,
        "finalPrice": 32991.0,
        "regCloseDate": "2024-03-12",
        "startDate": "2024-03-20",
        "endDate": "2024-04-28",
        "finalRegCloseDate": "2024-03-26",
        "websiteVisibility": "public",
    }

    SUCCESS_API_RESPONSE_MULTI_VARIANTS = copy.deepcopy(SUCCESS_API_RESPONSE)
    SUCCESS_API_RESPONSE_MULTI_VARIANTS['products'][0].pop('variant')
    SUCCESS_API_RESPONSE_MULTI_VARIANTS["products"][0].update({"variants": [variant_1, variant_2,]})
    SUCCESS_API_RESPONSE_MULTI_VARIANTS["products"][0].update({"edxTaxiFormId": None})

    SUCCESS_API_RESPONSE_CUSTOM_AND_FUTURE_VARIANTS = copy.deepcopy(SUCCESS_API_RESPONSE)
    SUCCESS_API_RESPONSE_CUSTOM_AND_FUTURE_VARIANTS['products'][0].update({
        'customPresentations': [{**copy.deepcopy(variant_1), 'websiteVisibility': 'private', 'status': 'active'}],
        'futureVariants': [
            {
                **copy.deepcopy(variant_2), 'websiteVisibility': 'public', 'status': 'scheduled',
                'startDate': '2026-03-20', 'endDate': '2026-04-28', 'finalRegCloseDate': '2026-03-26'
            }
        ]})

    def mock_product_api_call(self, override_product_api_response=None):
        """
        Mock product api with success response.
        """
        api_response = self.SUCCESS_API_RESPONSE
        if override_product_api_response:
            api_response = override_product_api_response
        responses.add(
            responses.GET,
            settings.PRODUCT_API_URL + '/?detail=2',
            body=json.dumps(api_response),
            status=200,
        )

    def mock_get_smarter_client_response(self, override_get_smarter_client_response=None):
        """
        Mock get_smarter_client response with success response.
        """
        if override_get_smarter_client_response:
            return override_get_smarter_client_response
        return self.SUCCESS_API_RESPONSE

    @mock.patch('course_discovery.apps.course_metadata.utils.GetSmarterEnterpriseApiClient')
    def test_successful_file_data_population_with_getsmarter_flag(self, mock_get_smarter_client):
        """
        Verify the successful population has data from API response if getsmarter flag is provided.
        """
        mock_get_smarter_client.return_value.request.return_value.json.return_value = self.mock_get_smarter_client_response()  # pylint: disable=line-too-long
        with LogCapture(LOGGER_PATH) as log_capture:
            output_csv = NamedTemporaryFile()  # lint-amnesty, pylint: disable=consider-using-with
            call_command(
                'populate_executive_education_data_csv',
                '--output_csv', output_csv.name,
                '--use_getsmarter_api_client', True,
            )
            output_csv.seek(0)
            reader = csv.DictReader(open(output_csv.name, 'r'))  # lint-amnesty, pylint: disable=consider-using-with
            data_row = next(reader)
            self._assert_api_response(data_row)
            log_capture.check_present(
                (
                    LOGGER_PATH,
                    'INFO',
                    'Data population and transformation completed for CSV row title CSV Course'
                ),
            )

    @mock.patch("course_discovery.apps.course_metadata.utils.GetSmarterEnterpriseApiClient")
    def test_skip_products_ingestion_if_variants_data_empty(self, mock_get_smarter_client):
        """
        Verify that the command skips the product ingestion if the variants data is empty
        """
        success_api_response = copy.deepcopy(self.SUCCESS_API_RESPONSE_MULTI_VARIANTS)
        success_api_response["products"][0]["variants"] = []
        mock_get_smarter_client.return_value.request.return_value.json.return_value = (
            self.mock_get_smarter_client_response(
                override_get_smarter_client_response=success_api_response
            )
        )
        with NamedTemporaryFile() as output_csv:
            with LogCapture(LOGGER_PATH) as log_capture:
                call_command(
                    "populate_executive_education_data_csv",
                    "--output_csv",
                    output_csv.name,
                    "--use_getsmarter_api_client",
                    True,
                )
                log_capture.check_present(
                    (
                        LOGGER_PATH,
                        "WARNING",
                        f"Skipping product {success_api_response['products'][0]['name']} "
                        f"ingestion as it has no variants",
                    ),
                )

                output_csv.seek(0)
                with open(output_csv.name, "r") as csv_file:
                    reader = csv.DictReader(csv_file)
                    assert not any(reader)

    @mock.patch("course_discovery.apps.course_metadata.utils.GetSmarterEnterpriseApiClient")
    def test_populate_executive_education_data_csv_with_new_variants_structure_changes(
        self, mock_get_smarter_client
    ):
        """
        Verify the successful population has data from API response if getsmarter flag is provided and
        the product can have multiple variants
        """
        success_api_response = copy.deepcopy(
            self.SUCCESS_API_RESPONSE_CUSTOM_AND_FUTURE_VARIANTS
        )
        mock_get_smarter_client.return_value.request.return_value.json.return_value = (
            self.mock_get_smarter_client_response(
                override_get_smarter_client_response=success_api_response
            )
        )
        with NamedTemporaryFile() as output_csv:
            call_command(
                "populate_executive_education_data_csv",
                "--output_csv",
                output_csv.name,
                "--use_getsmarter_api_client",
                True,
            )

            simple_variant = self.SUCCESS_API_RESPONSE_CUSTOM_AND_FUTURE_VARIANTS["products"][0]["variant"]
            future_variant = self.SUCCESS_API_RESPONSE_CUSTOM_AND_FUTURE_VARIANTS["products"][0]["futureVariants"][0]
            custom_variant = self.SUCCESS_API_RESPONSE_CUSTOM_AND_FUTURE_VARIANTS[
                "products"
            ][0]["customPresentations"][0]

            with open(output_csv.name, "r") as csv_file:
                reader = csv.DictReader(csv_file)

                data_row = next(reader)
                assert data_row["Variant Id"] == simple_variant["id"]
                assert data_row["Start Date"] == simple_variant["startDate"]
                assert data_row["End Date"] == simple_variant["endDate"]
                assert data_row["Reg Close Date"] == simple_variant["finalRegCloseDate"]
                assert data_row["Restriction Type"] == "None"
                assert data_row["Is Future Variant"] == "False"

                data_row = next(reader)
                assert data_row["Variant Id"] == future_variant["id"]
                assert data_row["Start Date"] == future_variant["startDate"]
                assert data_row["End Date"] == future_variant["endDate"]
                assert data_row["Reg Close Date"] == future_variant["finalRegCloseDate"]
                assert data_row["Publish Date"] == future_variant["startDate"]
                assert data_row["Restriction Type"] == "None"
                assert data_row["Is Future Variant"] == "True"

                data_row = next(reader)
                assert data_row["Variant Id"] == custom_variant["id"]
                assert data_row["Start Date"] == custom_variant["startDate"]
                assert data_row["End Date"] == custom_variant["endDate"]
                assert data_row["Reg Close Date"] == custom_variant["finalRegCloseDate"]
                assert data_row["Publish Date"] == str(date.today().isoformat())
                assert data_row["Restriction Type"] == "custom-b2b-enterprise"
                assert data_row["Is Future Variant"] == "False"

    @mock.patch('course_discovery.apps.course_metadata.utils.GetSmarterEnterpriseApiClient')
    def test_successful_file_data_population_with_getsmarter_flag_with_multiple_variants(self, mock_get_smarter_client):
        """
        Verify the successful population has data from API response if getsmarter flag is provided and
        the product can have multiple variants
        """
        mock_get_smarter_client.return_value.request.return_value.json.return_value = (
            self.mock_get_smarter_client_response(
                override_get_smarter_client_response=self.SUCCESS_API_RESPONSE_MULTI_VARIANTS
            )
        )
        with NamedTemporaryFile() as output_csv:
            with LogCapture(LOGGER_PATH) as log_capture:
                call_command(
                    'populate_executive_education_data_csv',
                    '--output_csv', output_csv.name,
                    '--use_getsmarter_api_client', True,
                )

            output_csv.seek(0)
            with open(output_csv.name, 'r') as csv_file:
                reader = csv.DictReader(csv_file)
                data_row = next(reader)
                assert data_row['Variant Id'] == self.variant_1['id']
                assert data_row['Start Time'] == '00:00:00'
                assert data_row['Start Date'] == self.variant_1['startDate']
                assert data_row['End Time'] == '00:00:00'
                assert data_row['End Date'] == self.variant_1['endDate']
                assert data_row['Reg Close Date'] == self.variant_1['finalRegCloseDate']
                assert data_row['Reg Close Time'] == '00:00:00'
                assert data_row['Verified Price'] == str(self.variant_1['finalPrice'])
                assert data_row['Restriction Type'] == 'custom-b2b-enterprise'
                assert data_row['Fixed Price Usd'] == '3510.0'
                assert data_row['Taxi Form Id'] == ''
                assert data_row['Post Submit Url'] == 'https://www.getsmarter.com/blog/career-advice'

                data_row = next(reader)
                assert data_row['Variant Id'] == self.variant_2['id']
                assert data_row['Start Time'] == '00:00:00'
                assert data_row['Start Date'] == self.variant_2['startDate']
                assert data_row['End Time'] == '00:00:00'
                assert data_row['End Date'] == self.variant_2['endDate']
                assert data_row['Reg Close Date'] == self.variant_2['finalRegCloseDate']
                assert data_row['Reg Close Time'] == '00:00:00'
                assert data_row['Verified Price'] == str(self.variant_2['finalPrice'])
                assert data_row['Restriction Type'] == 'None'
                assert data_row['Fixed Price Usd'] == ''
                assert data_row['Taxi Form Id'] == ''
                assert data_row['Post Submit Url'] == 'https://www.getsmarter.com/blog/career-advice'

            log_capture.check_present(
                (
                    LOGGER_PATH,
                    'INFO',
                    'Data population and transformation completed for CSV row title CSV Course'
                ),
            )

    @mock.patch('course_discovery.apps.course_metadata.utils.GetSmarterEnterpriseApiClient')
    def test_taxi_form_post_submit_url_in_case_prospectus_url_is_empty_and_product_type_sprint(
        self, mock_get_smarter_client
    ):
        """
        Verify that the taxi form post submit url is set to the edxPlpUrl if the prospectusUrl is empty
        and the product type is sprint.
        """
        success_api_response = copy.deepcopy(self.SUCCESS_API_RESPONSE)
        success_api_response['products'][0]['prospectusUrl'] = ''
        success_api_response['products'][0]['productType'] = 'sprint'
        success_api_response['products'][0]['edxPlpUrl'] = 'https://example.com/presentations/lp/example-course/'
        mock_get_smarter_client.return_value.request.return_value.json.return_value = (
            self.mock_get_smarter_client_response(
                override_get_smarter_client_response=success_api_response
            )
        )
        with NamedTemporaryFile() as output_csv:
            call_command(
                'populate_executive_education_data_csv',
                '--output_csv', output_csv.name,
                '--use_getsmarter_api_client', True,
            )

            output_csv.seek(0)
            with open(output_csv.name, 'r') as csv_file:
                reader = csv.DictReader(csv_file)
                data_row = next(reader)
                assert data_row['Title'] == 'Alternative CSV Course'
                assert data_row['External Course Marketing Type'] == 'sprint'
                assert data_row['Taxi Form Id'] == 'test-form-id'
                assert data_row['Post Submit Url'] == 'https://example.com/presentations/info/example-course/'

    @mock.patch("course_discovery.apps.course_metadata.utils.GetSmarterEnterpriseApiClient")
    @ddt.data(
        ("active", str(date.today().isoformat())), ("scheduled", "2024-03-20")
    )
    @ddt.unpack
    def test_successful_file_data_population_with_getsmarter_flag_with_future_variants(
        self,
        variant_status,
        expected_publish_date,
        mock_get_smarter_client,
    ):
        """
        Verify that data is correctly populated from the API response when the getsmarter flag is enabled.
        If a variant is scheduled, its publish date is set to the start date. If the variant is active,
        the publish date is set to the current date.
        """
        success_api_response = copy.deepcopy(self.SUCCESS_API_RESPONSE_MULTI_VARIANTS)
        success_api_response["products"][0]["variants"][0]["status"] = variant_status
        success_api_response["products"][0]["variants"][1]["status"] = variant_status

        mock_get_smarter_client.return_value.request.return_value.json.return_value = (
            self.mock_get_smarter_client_response(
                override_get_smarter_client_response=success_api_response
            )
        )

        with NamedTemporaryFile() as output_csv:
            call_command(
                'populate_executive_education_data_csv',
                '--output_csv', output_csv.name,
                '--use_getsmarter_api_client', True,
            )

            output_csv.seek(0)
            with open(output_csv.name, 'r') as csv_file:
                reader = csv.DictReader(csv_file)

                data_row = next(reader)
                assert data_row['Publish Date'] == expected_publish_date

                data_row = next(reader)
                assert data_row['Publish Date'] == expected_publish_date

    @responses.activate
    def test_successful_file_data_population_with_input_csv(self):
        """
        Verify the successful population has data from both input CSV and API response if input csv is provided.
        """
        self.mock_product_api_call()

        with NamedTemporaryFile() as input_csv:
            input_csv = self._write_csv(input_csv, [mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT])

            with LogCapture(LOGGER_PATH) as log_capture:
                output_csv = NamedTemporaryFile()  # lint-amnesty, pylint: disable=consider-using-with
                call_command(
                    'populate_executive_education_data_csv',
                    '--input_csv', input_csv.name,
                    '--output_csv', output_csv.name,
                    '--auth_token', self.AUTH_TOKEN
                )
                output_csv.seek(0)
                reader = csv.DictReader(open(output_csv.name, 'r'))  # lint-amnesty, pylint: disable=consider-using-with
                data_row = next(reader)

                # Asserting certain data items to verify that both CSV and API
                # responses are present in the final CSV
                assert data_row['Organization Short Code Override'] == 'altEdx'
                assert data_row['External Identifier'] == '12345678'
                assert data_row['Start Time'] == '00:00:00'
                assert data_row['Short Description'] == 'A short description for CSV course'
                assert data_row['Long Description'] == 'Very short description\n' \
                                                       'This is supposed to be a long description'
                assert data_row['End Time'] == '00:00:00'
                assert data_row['Reg Close Date'] == '01/25/2050'
                assert data_row['Reg Close Time'] == '00:00:00'
                assert data_row['Course Enrollment Track'] == 'Executive Education(2U)'
                assert data_row['Course Run Enrollment Track'] == 'Unpaid Executive Education'
                assert data_row['Length'] == '10'
                assert data_row['Number'] == 'TC'
                assert data_row['Redirect Url'] == 'https://example.com/'
                assert data_row['Organic Url'] == 'https://example.com/'
                assert data_row['Image'] == 'https://example.com/image.jpg'
                assert data_row['Organization Logo Override'] == 'https://example.com/image.jpg'
                assert data_row['Course Level'] == 'Introductory'
                assert data_row['Course Pacing'] == 'Instructor-Paced'
                assert data_row['Content Language'] == 'Spanish - Spain (Modern)'
                assert data_row['Transcript Language'] == 'Spanish - Spain (Modern)'
                assert data_row['Primary Subject'] == 'Design and Marketing'
                assert data_row['Frequently Asked Questions'] == '<div><p><b>FAQ 1</b></p>This should answer it</div>'
                assert data_row['Syllabus'] == '<div><p>Test Curriculum</p><p><b>Module 0: </b>Welcome to your course' \
                                               '</p><p><b>Module 1: </b>Welcome to Module 1</p></div>'
                assert data_row['Learner Testimonials'] == '<div><p><i>" This is a good course"</i></p><p>-Lorem ' \
                                                           'Ipsum (Gibberish)</p></div>'
                assert str(date.today().year) in data_row['Publish Date']
                assert data_row['Restriction Type'] == 'None'

                log_capture.check_present(
                    (
                        LOGGER_PATH,
                        'INFO',
                        'Data population and transformation completed for CSV row title CSV Course'
                    ),
                )

    @responses.activate
    def test_successful_file_data_population_without_input_csv(self):
        """
        Verify the successful population has data from API response only if optional input csv is not provided.
        """
        self.mock_product_api_call()

        with LogCapture(LOGGER_PATH) as log_capture:
            output_csv = NamedTemporaryFile()  # lint-amnesty, pylint: disable=consider-using-with
            call_command(
                'populate_executive_education_data_csv',
                '--output_csv', output_csv.name,
                '--auth_token', self.AUTH_TOKEN
            )
            output_csv.seek(0)
            reader = csv.DictReader(open(output_csv.name, 'r'))  # lint-amnesty, pylint: disable=consider-using-with
            data_row = next(reader)

            self._assert_api_response(data_row)

            log_capture.check_present(
                (
                    LOGGER_PATH,
                    'INFO',
                    'Data population and transformation completed for CSV row title CSV Course'
                ),
            )

    @responses.activate
    def test_successful_file_data_population_input_csv_no_product_info(self):
        """
        Verify the successful population has data from API response only if optional input csv does not have
        the details of a particular product.
        """
        self.mock_product_api_call()
        mismatched_product = {
            **mock_data.VALID_COURSE_AND_COURSE_RUN_CSV_DICT,
            'title': 'Not present in CSV'
        }
        with NamedTemporaryFile() as input_csv:
            input_csv = self._write_csv(input_csv, [mismatched_product])

            with LogCapture(LOGGER_PATH) as log_capture:
                output_csv = NamedTemporaryFile()  # lint-amnesty, pylint: disable=consider-using-with
                call_command(
                    'populate_executive_education_data_csv',
                    '--input_csv', input_csv.name,
                    '--output_csv', output_csv.name,
                    '--auth_token', self.AUTH_TOKEN
                )

                output_csv.seek(0)
                reader = csv.DictReader(open(output_csv.name, 'r'))  # lint-amnesty, pylint: disable=consider-using-with
                data_row = next(reader)

                self._assert_api_response(data_row)

                log_capture.check_present(
                    (
                        LOGGER_PATH,
                        'INFO',
                        'Data population and transformation completed for CSV row title CSV Course'
                    ),
                    (
                        LOGGER_PATH,
                        'WARNING',
                        '[MISSING PRODUCT IN CSV] Unable to find product details for product CSV Course in CSV'
                    ),
                )

    def test_invalid_csv_path(self):
        """
        Test that the command raises CommandError if an invalid csv path is provided.
        """
        with self.assertRaisesMessage(
                CommandError, 'Error opening csv file at path /tmp/invalid_csv.csv'
        ):
            output_csv = NamedTemporaryFile()  # lint-amnesty, pylint: disable=consider-using-with
            call_command(
                'populate_executive_education_data_csv',
                '--input_csv', '/tmp/invalid_csv.csv',
                '--output_csv', output_csv.name,
                '--auth_token', self.AUTH_TOKEN
            )

    def test_missing_json_and_auth_token(self):
        """
        Test that the command raises CommandError if both auth token and input JSON are missing.
        """
        with self.assertRaisesMessage(
                CommandError,
                'auth_token or dev_input_json or getsmarter_flag should be provided to perform data transformation.'
        ):
            output_csv = NamedTemporaryFile()  # lint-amnesty, pylint: disable=consider-using-with
            call_command(
                'populate_executive_education_data_csv',
                '--output_csv', output_csv.name,
            )

    @responses.activate
    def test_product_api_call_failure(self):
        """
        Test the command raises an error if the product API call fails for some reason.
        """
        responses.add(
            responses.GET,
            settings.PRODUCT_API_URL + '/?detail=2',
            status=400,
        )
        with self.assertRaisesMessage(
                CommandError, 'Unexpected error occurred while fetching products'
        ):
            csv_file = NamedTemporaryFile()  # lint-amnesty, pylint: disable=consider-using-with
            call_command(
                'populate_executive_education_data_csv',
                '--input_csv', csv_file.name,
                '--output_csv', csv_file.name,
                '--auth_token', self.AUTH_TOKEN
            )

    def _assert_api_response(self, data_row):
        """
        Assert the default API response in output CSV dict.
        """
        # pylint: disable=too-many-statements
        assert data_row['Organization Short Code Override'] == 'altEdx'
        assert data_row['2U Organization Code'] == 'edX'
        assert data_row['Number'] == 'TC'
        assert data_row['Alternate Number'] == 'UCT'
        assert data_row['Title'] == 'Alternative CSV Course'
        assert data_row['2U Title'] == 'CSV Course'
        assert data_row['Edx Title'] == 'Alternative CSV Course'
        assert data_row['2U Primary Subject'] == 'Marketing'
        assert data_row['Primary Subject'] == 'Design and Marketing'
        assert data_row['Subject Subcategory'] == 'Marketing, Sales, and Techniques'
        assert data_row['External Identifier'] == '12345678'
        assert data_row['Start Time'] == '00:00:00'
        assert data_row['Start Date'] == '2022-03-06'
        assert data_row['End Time'] == '00:00:00'
        assert data_row['End Date'] == '2022-05-06'
        assert data_row['Reg Close Date'] == '2022-02-15'
        assert data_row['Reg Close Time'] == '00:00:00'
        assert data_row['Verified Price'] == '1998'
        assert data_row['Short Description'] == 'A short description for CSV course'
        assert data_row['Long Description'] == 'Very short description\n' \
                                               'This is supposed to be a long description'
        assert data_row['Course Enrollment Track'] == 'Executive Education(2U)'
        assert data_row['Course Run Enrollment Track'] == 'Unpaid Executive Education'
        assert data_row['Lead Capture Form Url'] == "www.example.com/lead-capture?id=123"
        assert data_row['Certificate Header'] == "About the certificate"
        assert data_row['Certificate Text'] == 'how this makes you special'
        assert data_row['Stat1'] == '90%'
        assert data_row['Stat1 Text'] == '<p>A vast number of special beings take this course</p>'
        assert data_row['Stat2'] == '100 million'
        assert data_row['Stat2 Text'] == '<p>VC fund</p>'
        assert data_row['Length'] == '10'
        assert data_row['Redirect Url'] == 'https://example.com/'
        assert data_row['Organic Url'] == 'https://example.com/'
        assert data_row['Image'] == 'https://example.com/image.jpg'
        assert data_row['Course Level'] == 'Introductory'
        assert data_row['Course Pacing'] == 'Instructor-Paced'
        assert data_row['Content Language'] == 'Spanish - Spain (Modern)'
        assert data_row['Transcript Language'] == 'Spanish - Spain (Modern)'

        assert data_row['Frequently Asked Questions'] == '<div><p><b>FAQ 1</b></p>This should answer it</div>'
        assert data_row['Syllabus'] == '<div><p>Test Curriculum</p><p><b>Module 0: </b>Welcome to your course' \
                                       '</p><p><b>Module 1: </b>Welcome to Module 1</p></div>'
        assert data_row['Learner Testimonials'] == '<div><p><i>" This is a good course"</i></p><p>-Lorem ' \
                                                   'Ipsum (Gibberish)</p></div>'
        assert str(date.today().year) in data_row['Publish Date']
        assert data_row['Variant Id'] == '00000000-0000-0000-0000-000000000000'
        assert data_row['Meta Title'] == 'SEO Title'
        assert data_row['Meta Description'] == 'SEO Description'
        assert data_row['Meta Keywords'] == 'Keyword 1, Keyword 2'
        assert data_row['Slug'] == 'csv-course-slug'
        assert data_row['External Course Marketing Type'] == "short_course"
        assert data_row['Fixed Price Usd'] == "333.3"
        assert data_row['Taxi Form Id'] == 'test-form-id'
        assert data_row['Post Submit Url'] == 'https://www.getsmarter.com/blog/career-advice'
        assert data_row['Is Future Variant'] == 'False'
