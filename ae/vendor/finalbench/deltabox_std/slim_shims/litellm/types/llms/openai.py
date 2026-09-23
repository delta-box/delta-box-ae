from __future__ import annotations

from typing import Any


class _Message(dict):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as e:
            raise AttributeError(name) from e


AllMessageValues = dict
ChatCompletionMessage = _Message
ChatCompletionSystemMessage = _Message
ChatCompletionUserMessage = _Message
ChatCompletionAssistantMessage = _Message
ChatCompletionToolMessage = _Message
