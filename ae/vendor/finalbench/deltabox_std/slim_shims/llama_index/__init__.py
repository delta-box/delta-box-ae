"""Keep installed LlamaIndex subpackages available to real parser/tokenizer code.

Index execution is redirected by sitecustomize; an empty regular package here
must not hide the installed llama_index.core needed by modern Moatless parsers.
"""
from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)
