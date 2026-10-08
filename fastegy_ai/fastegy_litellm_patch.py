"""FastEgy patches for LiteLLM.

1) Groq stream errors that carry a text code.
Groq ends a stream with {"error": {"code": "tool_use_failed", ...}} when gpt-oss writes a
malformed tool call ("parameters for tool web_search did not match schema", "Tool choice is
none, but model called a tool"). LiteLLM 1.82 uses that code as the HTTP status, and
int("tool_use_failed") crashes its mid-stream fallback, so the chat aborts instead of moving
to fastegy-fallback. Mapping a text code to 503 lets the router fall back as configured.

2) Gemini fallback in the middle of a conversation with tool calls.
Gemini 3 rejects earlier tool calls that carry no thought signature ("Function call is missing
a thought_signature"), which is always the case for calls written by gpt-oss. LiteLLM then sends
Google's documented placeholder signature, but only when the model name contains "gemini-3";
our fallback is "gemini-flash-lite-latest", which points at Gemini 3 without saying so. The
placeholder is now also sent for Gemini "-latest" names; other model names are unchanged.

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

try:
    from litellm.litellm_core_utils.prompt_templates import factory as _factory

    _original_signature = _factory._get_thought_signature_from_tool

    def _thought_signature(tool, model=None):
        signature = _original_signature(tool, model=model)
        if not signature and model and "gemini" in str(model) and "latest" in str(model):
            signature = _factory._get_dummy_thought_signature()
        return signature

    _factory._get_thought_signature_from_tool = _thought_signature
    print("FASTEGY_PATCH active: placeholder thought signature for Gemini tool history", flush=True)
except Exception as e:  # never stop LiteLLM from starting
    print("FASTEGY_PATCH skipped (gemini): %s" % e, flush=True)


class FastEgyPatch(CustomLogger):
    pass


proxy_handler_instance = FastEgyPatch()
