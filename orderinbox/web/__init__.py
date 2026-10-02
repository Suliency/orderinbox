"""Web subpackage."""
from .app import create_app
from .app_context import AppContext

__all__ = ["create_app", "AppContext"]
