"""FastEgy patch for LiteLLM: Groq stream errors that carry a text code.

Groq ends a stream with {"error": {"code": "tool_use_failed", ...}} when gpt-oss writes a
malformed tool call ("parameters for tool web_search did not match schema", "Tool choice is
none, but model called a tool"). LiteLLM 1.82 uses that code as the HTTP status, and
int("tool_use_failed") crashes its mid-stream fallback, so the chat aborts instead of moving
to fastegy-fallback. Mapping a text code to 503 lets the router fall back as configured.

Loaded by LiteLLM through litellm_settings.callbacks; the patch is applied on import.
If a future LiteLLM renames these internals, the patch skips itself and LiteLLM starts as usual.
"""
from litellm.integrations.custom_logger import CustomLogger

try:
    from litellm.llms.groq.chat import transformation as _groq
    from litellm.llms.openai.common_utils import OpenAIError

    _original_chunk_parser = _groq.GroqChatCompletionStreamingHandler.chunk_parser

    def _chunk_parser(self, chunk):
        error = chunk.get("error") if isinstance(chunk, dict) else None
        if isinstance(error, dict) and not isinstance(error.get("code"), int):
            raise OpenAIError(status_code=503, body=error,
                              message="groq %s: %s" % (error.get("code"), error.get("message")))
        return _original_chunk_parser(self, chunk)

    _groq.GroqChatCompletionStreamingHandler.chunk_parser = _chunk_parser
    print("FASTEGY_PATCH active: Groq stream error codes mapped to 503", flush=True)
except Exception as e:  # never stop LiteLLM from starting
    print("FASTEGY_PATCH skipped: %s" % e, flush=True)


class FastEgyPatch(CustomLogger):
    pass


proxy_handler_instance = FastEgyPatch()
