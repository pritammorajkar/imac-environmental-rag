"""Optional LLM access behind one small interface (OpenAI-compatible chat API).

Used only when LLM_PROVIDER is gemini or openai. What leaves the machine: the question plus the
retrieved report excerpts, and (only with `ingest --vision`) cropped report images.
"""

import base64
import json
import re

from .config import Settings


class LLMError(RuntimeError):
    pass


def parse_json(text: str) -> dict:
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    raise LLMError(f"model did not return valid JSON: {text[:200]!r}")


class OpenAICompatibleLLM:
    def __init__(self, settings: Settings):
        from openai import OpenAI

        self.client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url, timeout=90, max_retries=2)
        self.model = settings.llm_model
        self.name = f"{settings.llm_provider}:{settings.llm_model}"

    def _json_call(self, messages: list[dict]) -> dict:
        response = self.client.chat.completions.create(
            model=self.model, messages=messages, response_format={"type": "json_object"}
        )
        return parse_json(response.choices[0].message.content)

    def complete_json(self, system: str, user: str) -> dict:
        return self._json_call([{"role": "system", "content": system}, {"role": "user", "content": user}])

    def describe_image(self, png: bytes, prompt: str) -> dict:
        url = "data:image/png;base64," + base64.b64encode(png).decode()
        return self._json_call(
            [{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": url}}]}]
        )


def get_llm(settings: Settings) -> OpenAICompatibleLLM | None:
    return OpenAICompatibleLLM(settings) if settings.llm_enabled else None
