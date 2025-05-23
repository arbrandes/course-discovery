import base64
import functools
import logging
import math
from urllib.parse import parse_qsl, urlencode, urljoin

from django.conf import settings
from django.core.files.base import ContentFile
from django.db.models.fields.related import ManyToManyField
from django.utils.translation import gettext as _
from edx_django_utils.cache import RequestCache
from opaque_keys.edx.keys import CourseKey
from requests.exceptions import HTTPError
from sortedm2m.fields import SortedManyToManyField

from course_discovery.apps.core.api_client.lms import LMSAPIClient
from course_discovery.apps.core.utils import serialize_datetime
from course_discovery.apps.course_metadata.choices import CourseRunRestrictionType
from course_discovery.apps.course_metadata.models import CourseRun, CourseRunType, CourseType

logger = logging.getLogger(__name__)


def cast2int(value, name):
    """
    Attempt to cast the provided value to an integer.

    Arguments:
        value (str): A value to cast to an integer.
        name (str): A name to log if casting fails.

    Raises:
        ValueError, if the provided value can't be converted. A helpful
            error message is logged first.

    Returns:
        int | None
    """
    if value is None:
        return value

    try:
        return int(value)
    except ValueError:
        logger.exception('The "%s" parameter requires an integer value. "%s" is invalid.', name, value)
        raise


def get_query_param(request, name):
    """
    Get a query parameter and cast it to an integer.
    """
    # This facilitates DRF's schema generation. For more, see
    # https://github.com/encode/django-rest-framework/blob/3.6.3/rest_framework/schemas.py#L383
    if request is None:
        return None

    return cast2int(request.query_params.get(name), name)


def update_query_params_with_body_data(func_to_decorate):
    """
    Update Request query parameters with Request body data.

    Make merging when body data become query parameters.
    Solves the problem when it is impossible to pass the required parameters
    through query url string due to size problem and need to use body(json) data.

    Should be used only for Django View classes.

    BE AWARE: The decorator changes a state of Request object.
    """

    @functools.wraps(func_to_decorate)
    def wrapper(self, request, *args, **kwargs):
        _data = request.data.copy()
        for key, value in _data.items():
            if isinstance(value, (list, tuple)) and len(value) == 1:
                _data[key] = value[0]

        encoded_data = urlencode(_data, True)
        _mutable = request.query_params._mutable  # pylint: disable=protected-access
        request.query_params._mutable = True  # pylint: disable=protected-access
        for key, value in parse_qsl(encoded_data):
            request.query_params.appendlist(key, value)
        request.query_params._mutable = _mutable  # pylint: disable=protected-access

        return func_to_decorate(self, request, *args, **kwargs)

    return wrapper


def reviewable_data_has_changed(obj, new_key_vals, exempt_fields=None):
    """
    Check whether serialized data for the object has changed.

    Args:
        obj (Object): Object representing the persisted state
        new_key_vals (dict_items): List of (key,value) tuples representing the new state
        exempt_fields (list): List of field names where a change does not affect review status

    Returns:
        list of changed field names
    """
    changed = False
    changed_fields = []
    exempt_fields = exempt_fields or []
    for key, new_value in [x for x in new_key_vals if x[0] not in exempt_fields]:
        original_value = getattr(obj, key, None)
        if isinstance(new_value, list):
            field_class = obj.__class__._meta.get_field(key).__class__
            original_value_elements = original_value.all()
            if len(new_value) != original_value_elements.count():
                changed = True
            # Just use set compare since none of our fields require duplicates
            elif field_class == ManyToManyField and set(new_value) != set(original_value_elements):
                changed = True
            elif field_class == SortedManyToManyField:
                for new_value_element, original_value_element in zip(new_value, original_value_elements):
                    if new_value_element != original_value_element:
                        changed = True
        elif new_value != original_value:
            changed = True
        else:
            changed = False

        if changed:
            changed_fields.append(key)

    return changed_fields


def conditional_decorator(condition_getter, decorator):
    """
    Util decorator that applies the given decorator only if the condition passes.
    The condition is evaluated at runtime via the `condition_getter` callable, allowing dynamic condition checking.
    If callable evaluates to `True`, the provided decorator is applied, otherwise, the function remains unmodified.
    """
    def wrapper(func):
        def wrapped(*args, **kwargs):
            if condition_getter():
                return decorator(func)(*args, **kwargs)
            return func(*args, **kwargs)
        return wrapped
    return wrapper


def decode_image_data(image_data):
    """
    Given a encoded base64 image, it will decode encoded image and
    return image name and decoded image_data
    """
    file_format, img_str = image_data.split(';base64,')  # format ~= data:image/X;base64,/xxxyyyzzz/
    ext = file_format.split('/')[-1]  # guess file extension
    image_data = ContentFile(base64.b64decode(img_str), name=f'tmp.{ext}')
    return image_data.name, image_data


def check_catalog_api_access(partner, user):
    """
    Uses LMSAPIClient to check the catalog api access for a
    given user

    Arguments:
        user (User): Django User.

    Returns:
        (dict): ApiAccessRequests for the given user.

    Example:
        {
            "id": 1,
            "created": "2017-09-25T08:37:05.872566Z",
            "modified": "2017-09-25T08:37:47.412496Z",
            "user": 5,
            "status": "approved",
            "website": "https://example.com/",
            "reason": "Example Reason",
            "company_name": "Example Inc",
            "company_address": "Example Address",
            "site": 1,
            "contacted": True
        }
    """
    lms_client = LMSAPIClient(partner)
    api_access_response = lms_client.get_api_access_request(user)
    return api_access_response


def increment_str(input_str):
    """
    Given a string, it will return its next combination by incrementing the last alphabet and handle all boundary cases
    ref link: https://gist.github.com/jlp78/f306afc919dc06c8ce156475fc9320bf
    example:
    1. given a string 'a' and it will return 'b'
    2. given a string 'z' and it will return 'aa'
    3. given a string 'az' and it will return 'ba'
    """
    lpart = input_str.rstrip('z')
    num_replacements = len(input_str) - len(lpart)
    new_str = lpart[:-1] + increment_character(lpart[-1]) if lpart else 'a'
    new_str += 'a' * num_replacements
    return new_str


def increment_character(character):
    """
    Given a character and it will return its next character using ASCII code
    """
    return chr(ord(character) + 1) if character != 'z' else 'a'


def get_excluded_restriction_types(request):
    include_restricted = request.query_params.get('include_restricted', '').split(',')
    return list(set(CourseRunRestrictionType.values) - set(include_restricted))


class StudioAPI:
    """
    A convenience class for talking to the Studio API - designed to allow subclassing by the publisher django app,
    so that they can use it for their own publisher CourseRun models, which are slightly different than the course
    metadata ones.
    """

    def __init__(self, partner):
        self._api = partner.oauth_api_client
        # In our unit tests, urljoin has trouble with a mock str object vs a real str, so we ensure a real string here.
        self._url = str(partner.studio_url)

    @classmethod
    def _get_next_run(cls, root, suffix, existing_runs):
        candidate = root + suffix

        if candidate in existing_runs:
            # If our candidate is an existing run, use the next letter in the alphabet as the
            # run suffix (e.g. 1T2017, 1T2017a, 1T2017b, ...).
            suffix = increment_str(suffix)
            return cls._get_next_run(root, suffix, existing_runs)

        return candidate

    @classmethod
    def calculate_course_run_key_run_value(cls, course_num, start):
        trimester = math.ceil(start.month / 4.)
        run = f'{trimester}T{start.year}'

        related_course_runs = CourseRun.everything.filter(key__contains=course_num).values_list('key', flat=True)
        related_course_runs = [CourseKey.from_string(key).run for key in related_course_runs]

        return cls._get_next_run(run, '', related_course_runs)

    @classmethod
    def generate_data_for_studio_api(cls, course_run, creating, user=None):
        editors = [editor.user for editor in course_run.course.editors.all()]
        key = CourseKey.from_string(course_run.key)

        # start, end, and pacing are not sent on updates - Studio is where users edit them
        start = course_run.start if creating else None
        end = course_run.end if creating else None
        pacing = course_run.pacing_type if creating else None
        enrollment_start = course_run.enrollment_start
        enrollment_end = course_run.enrollment_end

        if user:
            editors.append(user)

        if editors:
            team = [
                {
                    'user': user.username,
                    'role': 'instructor',
                }
                for user in editors
            ]
        else:
            team = []
            logger.warning('No course team admin specified for course [%s]. This may result in a Studio '
                           'course run being created without a course team.', key.course)

        data = {
            'title': course_run.title,
            'org': key.org,
            'number': key.course,
            'run': key.run,
            'team': team,
        }

        if pacing:
            data['pacing_type'] = pacing

        if start or end:
            data['schedule'] = {
                'start': serialize_datetime(start),
                'end': serialize_datetime(end),
            }

        if not creating and (enrollment_start or enrollment_end):
            # The dates are intentionally not allowed when creating the course run.
            # It is possible to send enrollment dates in API when the course run is being created.
            # But when the course run is created, in Studio or Discovery, the enrollment dates are not taken as input.
            # It is better to keep the flow consistent across places.
            # Allow sending enrollment start and end dates as part of Update only.
            data['schedule'] = {
                'enrollment_start': serialize_datetime(course_run.enrollment_start),
                'enrollment_end': serialize_datetime(course_run.enrollment_end),
            }
            logger.info(f"Enrollment information added to data {data} for course run {course_run.key}")

        return data

    def _make_studio_url(self, path):
        return urljoin(self._url, 'api/v1/' + path)

    def _request(self, method, path, **kwargs):
        url = self._make_studio_url(path)
        response = self._api.request(method, url, **kwargs)
        try:
            response.raise_for_status()
        except HTTPError as exc:
            # Add a content field as extra debugging info for logging above us. This used to be automatically added
            # by slumber, but now with requests module, we need to manually add it.
            exc.content = response.content
            raise exc

    def create_course_rerun_in_studio(self, course_run, old_course_run_key, user=None):
        data = self.generate_data_for_studio_api(course_run, creating=True, user=user)
        self._request('post', f'course_runs/{old_course_run_key}/rerun/', json=data)

    def create_course_run_in_studio(self, publisher_course_run, user=None):
        data = self.generate_data_for_studio_api(publisher_course_run, creating=True, user=user)
        self._request('post', 'course_runs/', json=data)

    def update_course_run_image_in_studio(self, course_run):
        course = course_run.course
        image = course.image

        if image:
            files = {'card_image': image}
            try:
                self._request('post', f'course_runs/{course_run.key}/images/', files=files)
            except Exception:  # pylint: disable=broad-except
                logger.exception(
                    _('An error occurred while setting the course run image for [{key}] in studio. All other fields '
                      'were successfully saved in Studio.').format(key=course_run.key)
                )
        else:
            logger.warning(
                'Card image for course run [%d] cannot be updated. The related course [%d] has no image defined.',
                course_run.id,
                course.id
            )

    def update_course_run_details_in_studio(self, course_run):
        data = self.generate_data_for_studio_api(course_run, creating=False)
        # NOTE: We use PATCH to avoid overwriting existing team data that may have been manually input in Studio.
        self._request('patch', f'course_runs/{course_run.key}/', json=data)

    def push_to_studio(self, course_run, create=False, old_course_run_key=None, user=None):
        if create and old_course_run_key:
            self.create_course_rerun_in_studio(course_run, old_course_run_key, user=user)
        elif create:
            self.create_course_run_in_studio(course_run, user=user)
        else:
            self.update_course_run_details_in_studio(course_run)

    def _update_end_date_in_studio(self, course_run):
        """
        We do not update end dates in studio after creation. However, while archiving a course, we
        may wish to set the end date in the past. This method serves that purpose.
        """
        data = self.generate_data_for_studio_api(course_run, creating=False)
        data.setdefault('schedule', {})
        for attr in ['end', 'enrollment_end']:
            attr_val = getattr(course_run, attr)
            if attr_val:
                data['schedule'][attr] = serialize_datetime(attr_val)
        self._request('patch', f'course_runs/{course_run.key}/', json=data)


def use_request_cache(cache_name, key_func):
    """
    This decorator can be used to cache the result of a function per request.
    It leverages the RequestCache from edx-django-utils for this purpose

    Args:
        cache_name (string): The identifier for the cache
        key_func (callable): The function used for computing cache keys. This will be called
                             with the same arguments as the function being decorated

    Example:
        @use_request_cache("find_factors_cache", lambda x: x)
        def find_factors(x):
            print("Looking for factors!")
            # Some expensive calculation
            print("Found the factors")

        Over the course of a single request,
        find_factors(12345)  # This will run the expensive calculation.
        find_factors(12345)  # This will skip the expensive calculation and return cached value.
    """
    def inner(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            cache = RequestCache(cache_name)
            cache_key = key_func(*args, **kwargs)
            cached_response = cache.get_cached_response(cache_key)
            if cached_response.is_found:
                return cached_response.value

            result = fn(*args, **kwargs)

            cache.set(cache_key, result)
            return result
        return wrapper
    return inner


@use_request_cache("retired_run_types_cache", lambda: "retired_run_types")
def get_retired_run_type_ids():
    return list(
        CourseRunType.objects.filter(slug__in=settings.RETIRED_RUN_TYPES).values_list('id', flat=True)
    )


@use_request_cache("retired_course_types_cache", lambda: "retired_course_types")
def get_retired_course_type_ids():
    return list(
        CourseType.objects.filter(slug__in=settings.RETIRED_COURSE_TYPES).values_list('id', flat=True)
    )
