""" Utilities for working with JWT during testing."""
from time import time

import jwt
from django.conf import settings


def generate_jwt_payload(user, payload=None):
    """Generate a valid JWT payload given a user."""
    now = int(time())
    ttl = 5
    jwt_payload = {
        'iss': settings.JWT_AUTH['JWT_ISSUER'],
        'aud': settings.JWT_AUTH['JWT_AUDIENCE'],
        'username': user.username,
        'email': user.email,
        'iat': now,
        'exp': now + ttl
    }
    if payload:
        jwt_payload.update(payload)
    return jwt_payload


def generate_jwt_token(payload):
    """Generate a valid JWT token for authenticated requests."""
    return jwt.encode(payload, settings.JWT_AUTH['JWT_SECRET_KEY'])


def generate_jwt_header(token):
    """Generate a valid JWT header given a token."""
    return f'JWT {token}'


def generate_jwt_header_for_user(user, payload=None):
    payload = generate_jwt_payload(user, payload)
    token = generate_jwt_token(payload)

    return generate_jwt_header(token)
