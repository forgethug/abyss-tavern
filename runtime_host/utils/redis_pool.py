"""Optional Redis adapter; isolated beta runtimes fall back to DB/cache-less paths."""


def init(_redis_url: str, max_connections: int = 16) -> bool:
    del max_connections
    return False


def is_available() -> bool:
    return False


def get_client(*, decode_responses: bool = False):
    del decode_responses
    return None
