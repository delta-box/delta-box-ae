"""Worker-side proxy for a checkpoint-external CodeIndex sidecar."""
from __future__ import annotations

import json
import urllib.request
from typing import Any

from moatless.index.types import SearchCodeResponse
from moatless.schema import FileWithSpans


class SlimIndexProxy:
    def __init__(self, base_url: str, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, **kwargs: Any) -> Any:
        body = json.dumps(
            {"method": method, "kwargs": kwargs},
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/call",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            out = json.loads(resp.read())
        if not out.get("ok"):
            raise RuntimeError(out.get("error") or "index sidecar RPC failed")
        return out["result"]

    def semantic_search(self, **kwargs: Any) -> SearchCodeResponse:
        return SearchCodeResponse.model_validate(self._call("semantic_search", **kwargs))

    def find_class(self, class_name: str, file_pattern: str | None = None) -> SearchCodeResponse:
        return SearchCodeResponse.model_validate(
            self._call("find_class", class_name=class_name, file_pattern=file_pattern)
        )

    def find_function(
        self,
        function_name: str,
        class_name: str | None = None,
        file_pattern: str | None = None,
    ) -> SearchCodeResponse:
        return SearchCodeResponse.model_validate(
            self._call(
                "find_function",
                function_name=function_name,
                class_name=class_name,
                file_pattern=file_pattern,
            )
        )

    def find_test_files(self, **kwargs: Any) -> list[FileWithSpans]:
        return [FileWithSpans.model_validate(x) for x in self._call("find_test_files", **kwargs)]
