"""Late-binding proxy to the root `app` module.

Resolving shared state and helper functions via `core.<attr>` avoids circular
imports at Blueprint load time while preserving 100% compatibility with tests
that apply `unittest.mock.patch.object(app_module, ...)`.
"""

import sys


class _CoreProxy:
    def __getattr__(self, name: str):
        mod = sys.modules.get('app') or sys.modules.get('__main__')
        if mod is None:
            raise AttributeError(f"Root app module not loaded when resolving '{name}'")
        return getattr(mod, name)


core = _CoreProxy()
