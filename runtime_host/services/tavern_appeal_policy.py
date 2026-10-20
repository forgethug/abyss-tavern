"""Dispatch optional appeal policy to the active pack-owned extension."""
from flask import current_app


def _policy():
    return current_app.extensions['tavern_appeal_policy']


def explicit_clear_ids(patch):
    return _policy().explicit_clear_ids(patch)


def create_policy(*args, **kwargs):
    return _policy().create_policy(*args, **kwargs)


def payload(*args, **kwargs):
    return _policy().payload(*args, **kwargs)


def parse_policy(appeal):
    return _policy().parse_policy(appeal)


def finalize(appeal):
    return _policy().finalize(appeal)
