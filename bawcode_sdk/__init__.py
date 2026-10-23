"""BAWCode plugin SDK v1: host-independent contracts and context."""
API_VERSION = 1
__version__ = "1.0.0"

from .context import PluginContext
from .host import HostAPI, HostServices
from .ui import PluginUI

__all__ = ["API_VERSION", "PluginContext", "PluginUI", "HostAPI", "HostServices"]
