"""Startup validation shared by independently bootstrapped hosts."""

PUBLIC_EXAMPLES = (
    'replace-with-', 'replace-me', 'same-as-control-plane',
    'dev-runtime-secret', 'dev-change-me', 'dev-bootstrap-secret',
)


def validate_secret(name, value):
    if (not isinstance(value, str) or len(value) < 32 or not value.strip()
            or any(value.strip().lower().startswith(example) for example in PUBLIC_EXAMPLES)):
        raise RuntimeError(f'{name} must be a private value of at least 32 characters; run tools/configure.py')
    return value


def validate_control_secrets(config):
    for name in ('SECRET_KEY', 'JWT_SECRET_KEY', 'RUNTIME_BOOTSTRAP_SECRET'):
        validate_secret(name, config.get(name))
    if config['SECRET_KEY'] == config['JWT_SECRET_KEY']:
        raise RuntimeError('SECRET_KEY and JWT_SECRET_KEY must be different')
