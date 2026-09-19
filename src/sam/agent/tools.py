"""Tool contract for future bounded agent capabilities."""

from collections.abc import Mapping
from typing import Any, Protocol


class Tool(Protocol):
    """Description and execution contract for an agent tool."""

    name: str
    description: str

    def input_schema(self) -> Mapping[str, Any]:
        """Return the JSON-compatible input schema for this tool."""

    def execute(self, arguments: Mapping[str, Any]) -> Any:
        """Execute the tool with validated arguments."""