"""Claim and verify the immutable identity of one S1 gameplay database."""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import re
from dataclasses import dataclass
from datetime import datetime

from db_repository.decorators import db_retry, with_transaction
from db_repository.exceptions import DatabaseError, ValidationError
from db_repository.s1_deployment_identity_repository import (
    S1DeploymentIdentityRepository,
)


S1_DEPLOYMENT_SCHEMA_REVISION = 1
S1_DEPLOYMENT_ID_MAX_LENGTH = 100
VALID_S1_DEPLOYMENT_REALMS = frozenset({'region_1', 'region_2'})
_DEPLOYMENT_ID_PATTERN = re.compile(
    r'\A[a-z0-9](?:[a-z0-9._:-]{0,98}[a-z0-9])?\Z',
    re.ASCII,
)


class S1DeploymentIdentityValidationError(ValidationError):
    """The configured identity is not safe to persist or compare."""


class S1DeploymentIdentityConflict(DatabaseError):
    """The database has already been claimed by a different deployment."""


@dataclass(frozen=True)
class S1DeploymentIdentitySnapshot:
    realm_code: str
    deployment_id: str
    schema_revision: int
    created_at: datetime
    updated_at: datetime
    newly_claimed: bool


def validate_s1_deployment_id(value: object) -> str:
    """Return an exact safe deployment id without silently normalizing it."""
    if not isinstance(value, str):
        raise S1DeploymentIdentityValidationError(
            'S1 deployment id must be a string.',
            field='deployment_id',
            value=value,
        )
    if (
        len(value) > S1_DEPLOYMENT_ID_MAX_LENGTH
        or not _DEPLOYMENT_ID_PATTERN.fullmatch(value)
    ):
        raise S1DeploymentIdentityValidationError(
            'S1 deployment id must be 1-100 lowercase ASCII characters using '
            'letters, digits, dot, underscore, colon, or hyphen, with no '
            'whitespace or control characters.',
            field='deployment_id',
            value=value,
        )
    return value


def _validate_realm_code(value: object) -> str:
    if not isinstance(value, str) or value not in VALID_S1_DEPLOYMENT_REALMS:
        raise S1DeploymentIdentityValidationError(
            'S1 deployment realm must be region_1 or region_2.',
            field='realm_code',
            value=value,
        )
    return value


def _validate_schema_revision(value: object) -> int:
    if type(value) is not int or value < 1:
        raise S1DeploymentIdentityValidationError(
            'S1 deployment schema revision must be a positive integer.',
            field='schema_revision',
            value=value,
        )
    return value


@db_retry()
@with_transaction(auto_commit=True)
def claim_or_verify_s1_deployment_identity(
    *,
    realm_code: str,
    deployment_id: str,
    schema_revision: int = S1_DEPLOYMENT_SCHEMA_REVISION,
    now: datetime | None = None,
) -> S1DeploymentIdentitySnapshot:
    """Atomically create the singleton or verify the exact existing claim.

    No mismatch path updates the row. The transaction is rolled back on any
    malformed singleton, realm mismatch, deployment mismatch, or revision
    mismatch, so a database can never be silently reassigned.
    """
    expected_realm = _validate_realm_code(realm_code)
    expected_deployment = validate_s1_deployment_id(deployment_id)
    expected_revision = _validate_schema_revision(schema_revision)
    claimed_at = now or _portable_utc_now()

    newly_claimed = S1DeploymentIdentityRepository.insert_if_absent(
        realm_code=expected_realm,
        deployment_id=expected_deployment,
        schema_revision=expected_revision,
        now=claimed_at,
    )
    rows = S1DeploymentIdentityRepository.lock_all_bounded()
    if len(rows) != 1 or rows[0].id != 1:
        raise S1DeploymentIdentityConflict(
            'S1 deployment identity table is not a valid id=1 singleton.',
            operation='claim_or_verify_s1_deployment_identity',
        )

    row = rows[0]
    mismatches = []
    if row.realm_code != expected_realm:
        mismatches.append(
            f'realm_code expected {expected_realm}, found {row.realm_code}'
        )
    if row.deployment_id != expected_deployment:
        mismatches.append(
            'deployment_id expected '
            f'{expected_deployment}, found {row.deployment_id}'
        )
    if row.schema_revision != expected_revision:
        mismatches.append(
            'schema_revision expected '
            f'{expected_revision}, found {row.schema_revision}'
        )
    if mismatches:
        raise S1DeploymentIdentityConflict(
            'S1 database deployment identity conflict: ' + '; '.join(mismatches),
            operation='claim_or_verify_s1_deployment_identity',
        )

    return S1DeploymentIdentitySnapshot(
        realm_code=row.realm_code,
        deployment_id=row.deployment_id,
        schema_revision=row.schema_revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
        newly_claimed=newly_claimed,
    )
