class DatabaseError(Exception):
    def __init__(self, message: str, original_error: Exception = None, operation=None, model=None):
        super().__init__(message)
        self.original_error = original_error
        self.operation = operation
        self.model = model


class TransactionError(DatabaseError):
    pass


class ConcurrencyError(DatabaseError):
    pass


class DuplicateRecordError(DatabaseError):
    pass


class ValidationError(DatabaseError):
    def __init__(self, message, field=None, value=None):
        super().__init__(message)
        self.field = field
        self.value = value
