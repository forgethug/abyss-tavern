"""In-process evidence carried with the existing physical-call usage receipt.

Only the capture ID is JSON-visible. Request/response bodies stay in attributes
until the token writer archives a failed call or a field-name compatibility
diagnostic; ordinary metadata projections and successful business snapshots
therefore cannot accidentally copy full prompts.
"""
from copy import deepcopy
import json
import re
import traceback
from uuid import uuid4


S1_AI_FAILURE_EVIDENCE_KEY = '_s1_ai_failure_evidence'
_CREDENTIAL_FIELDS = frozenset({
    'authorization', 'proxy-authorization', 'api_key', 'apikey', 'api-key',
    'x-api-key', 'access_token', 'refresh_token', 'password', 'secret',
    'cookie', 'set-cookie',
})


class S1AiFailureEvidence(dict):
    """A shallow-copyable, JSON-safe handle, not an ambient request registry."""

    def __init__(self, request_payload: dict, *, api_key: str = ''):
        super().__init__(capture_id=uuid4().hex)
        self.request_payload = deepcopy(request_payload)
        self._api_key = str(api_key or '')
        self._chunks = bytearray()
        self.response_body = None
        self.response_format = 'unreceived'
        self.response_complete = False
        self.http_status = None
        self.errors = []
        self.contract_diagnostics = {}
        self.field_name_compatibility = []

    def record_field_name_compatibility(self, diagnostics):
        # Gateway validation and final consumption may parse the same response.
        if diagnostics not in self.field_name_compatibility:
            self.field_name_compatibility.append(deepcopy(diagnostics))

    def bind_response(self, response):
        setattr(response, S1_AI_FAILURE_EVIDENCE_KEY, self)
        status = getattr(response, 'status_code', None)
        self.http_status = status if type(status) is int else None
        self.capture_response(response)

    def append_chunk(self, chunk, *, stream=False):
        if isinstance(chunk, str):
            chunk = chunk.encode('utf-8')
        if isinstance(chunk, (bytes, bytearray)):
            self._chunks.extend(chunk)
            self.response_format = 'sse' if stream else 'http_body'

    def capture_response(self, response, *, complete=None):
        """Capture already received bytes only; never trigger a lazy body read."""
        def decode(raw):
            try:
                return raw.decode(getattr(response, 'encoding', None) or 'utf-8', errors='replace')
            except LookupError:
                return raw.decode('utf-8', errors='replace')

        if self._chunks or self.response_format == 'sse':
            self.response_body = decode(bytes(self._chunks))
        elif self.response_body is None:
            raw = getattr(response, '_content', None)
            body = (decode(raw)
                    if isinstance(raw, bytes) else
                    getattr(response, 'text', None)
                    if getattr(response, '_content_consumed', True) is not False else None)
            if isinstance(body, str):
                self.response_body = body
                self.response_format = 'http_body'
                self.response_complete = True
        if complete is not None:
            self.response_complete = bool(complete) and self.response_body is not None

    def capture_exception(self, exc):
        self.errors.append({
            'type': type(exc).__name__,
            'message': str(exc),
            'traceback': ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        })

    def _redact(self, value):
        if isinstance(value, dict):
            return {
                str(key): '[REDACTED]' if str(key).lower() in _CREDENTIAL_FIELDS
                else self._redact(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self._redact(item) for item in value]
        if isinstance(value, str):
            if self._api_key:
                value = value.replace(self._api_key, '[REDACTED]')
            # Exception messages can contain a URL, even though request URLs and
            # headers themselves are never part of the captured request.
            value = re.sub(r'(https?://)[^/\s@]+@', r'\1[REDACTED]@', value)
            value = re.sub(
                r'(?i)([?&](?:api_key|apikey|key|token|access_token|secret)=)[^&\s\"\']+',
                r'\1[REDACTED]', value,
            )
            return value
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return type(value).__name__

    def serialize(self, *, failure_stage=None, validation_error_code=None, error_excerpt=None):
        document = {
            'version': 1,
            'capture_id': self['capture_id'],
            'request_payload': self.request_payload,
            'response': {
                'http_status': self.http_status,
                'format': self.response_format,
                'complete': self.response_complete,
                'body': self.response_body,
            },
            'failure_stage': failure_stage,
            'validation_error_code': validation_error_code,
            'contract_diagnostics': self.contract_diagnostics,
            'field_name_compatibility': self.field_name_compatibility,
            'exceptions': self.errors,
            'owner_error': error_excerpt,
        }
        return json.dumps(self._redact(document), ensure_ascii=False)


def get_s1_ai_failure_evidence(source):
    value = (source.get(S1_AI_FAILURE_EVIDENCE_KEY) if isinstance(source, dict)
             else getattr(source, S1_AI_FAILURE_EVIDENCE_KEY, None))
    return value if isinstance(value, S1AiFailureEvidence) else None


def capture_s1_ai_response_chunk(response, chunk, *, stream=False):
    evidence = get_s1_ai_failure_evidence(response)
    if evidence is not None:
        evidence.append_chunk(chunk, stream=stream)


def capture_s1_ai_response(response, *, complete=None):
    evidence = get_s1_ai_failure_evidence(response)
    if evidence is not None:
        evidence.capture_response(response, complete=complete)


def capture_s1_ai_exception(source, exc):
    evidence = get_s1_ai_failure_evidence(source)
    if evidence is not None:
        evidence.capture_exception(exc)
