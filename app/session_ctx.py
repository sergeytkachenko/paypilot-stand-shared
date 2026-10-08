""
from contextlib import contextmanager
from contextvars import ContextVar

_customer: ContextVar[str | None] = ContextVar("session_customer", default=None)


def current() -> str | None:
    return _customer.get()


@contextmanager
def bind(customer_id: str | None):
    token = _customer.set(customer_id)
    try:
        yield
    finally:
        _customer.reset(token)
