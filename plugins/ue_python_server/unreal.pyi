 # unreal stub — UE's embedded Python module is only available inside the UE editor.
# This stub silences type checkers for the plugins/ folder.
from typing import Any

def log(msg: Any, ...) -> None: ...  # type: ignore[misc]
def log_warning(msg: Any, ...) -> None: ...  # type: ignore[misc]
def log_error(msg: Any, ...) -> None: ...  # type: ignore[misc]

class SystemLibrary:
    @staticmethod
    def get_engine_version() -> str: ...
