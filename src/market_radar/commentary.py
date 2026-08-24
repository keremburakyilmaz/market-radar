"""Optional server-side model rewrite for the deterministic daily commentary."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from market_radar.validation import validate_snapshot

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "meta/llama-3.3-70b-instruct"
COMMENTARY_METHOD = "daily-commentary-v1"
_SECTION_NAMES = ("dataRead", "newsRead", "watchNext")
_NUMBER_PATTERN = re.compile(r"(?<![A-Za-z0-9])[+-]?\d+(?:\.\d+)?%?(?![A-Za-z0-9])")

ModelRequest = Callable[[Request, float], bytes]


def _request(request: Request, timeout: float) -> bytes:
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL is HTTPS-validated
        return cast(bytes, response.read())


@dataclass(frozen=True)
class ModelCommentaryEnhancer:
    """Rewrite prose with an OpenAI-compatible chat model, then fail closed.

    Evidence selection stays deterministic. A model may rewrite only headlines
    and bodies; it cannot change evidence IDs, scores, stories, or calendar data.
    """

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: float = 45.0
    requester: ModelRequest = _request

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("commentary base URL must be credential-free HTTPS")
        if not self.api_key.strip():
            raise ValueError("commentary API key is required")
        if not self.model.strip() or len(self.model) > 80 or "<" in self.model or ">" in self.model:
            raise ValueError("commentary model name is invalid")
        if not 1 <= self.timeout_seconds <= 120:
            raise ValueError("commentary timeout must be between 1 and 120 seconds")

    def enhance(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Return model-assisted prose, or the untouched deterministic snapshot."""

        try:
            evidence = self._evidence(snapshot)
            requested = self._call_model(evidence)
            sections = self._validate_sections(requested, evidence)
            enhanced = copy.deepcopy(snapshot)
            commentary = enhanced["digest"]["commentary"]
            commentary["generation"] = {
                "mode": "model-assisted",
                "method": COMMENTARY_METHOD,
                "model": self.model,
            }
            for section_name in _SECTION_NAMES:
                commentary[section_name]["headline"] = sections[section_name]["headline"]
                commentary[section_name]["body"] = sections[section_name]["body"]
            validate_snapshot(enhanced)
            return enhanced
        except Exception:
            return snapshot

    @staticmethod
    def _evidence(snapshot: Mapping[str, Any]) -> dict[str, Any]:
        conditions = snapshot["macroConditions"]
        digest = snapshot["digest"]
        return {
            "period": {
                "start": digest["periodStart"],
                "end": digest["periodEnd"],
                "label": "24-hour daily briefing",
            },
            "conditions": {
                "score": conditions["score"],
                "label": conditions["label"],
                "summary": conditions["summary"],
                "scoreScale": conditions["scoreScale"],
                "drivers": conditions["drivers"],
            },
            "indicators": snapshot["indicators"],
            "stories": snapshot["stories"],
            "calendar": snapshot["calendar"],
            "deterministicCommentary": digest["commentary"],
        }

    def _call_model(self, evidence: Mapping[str, Any]) -> dict[str, Any]:
        system_prompt = (
            "You write Market Radar's daily market briefing. Rewrite three short sections using "
            "only the supplied evidence. Do not give investment advice or predict prices. Do not "
            "infer article content from a headline. Clearly distinguish official releases from "
            "unconfirmed discovery metadata. Keep every number exactly as written in the evidence. "
            "Return only a JSON object with dataRead, newsRead, and watchNext; each must contain "
            "headline and body strings. Each body should be two or three concise sentences."
        )
        body = json.dumps(
            {
                "model": self.model,
                "temperature": 0.1,
                "max_tokens": 700,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                    },
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        endpoint = f"{self.base_url.rstrip('/')}/chat/completions"
        request = Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        response = json.loads(self.requester(request, self.timeout_seconds).decode("utf-8"))
        content = response["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("commentary response content must be text")
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise TypeError("commentary response must be an object")
        return parsed

    @staticmethod
    def _validate_sections(
        payload: Mapping[str, Any], evidence: Mapping[str, Any]
    ) -> dict[str, dict[str, str]]:
        if set(payload) != set(_SECTION_NAMES):
            raise ValueError("commentary response has unexpected sections")
        evidence_text = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        output: dict[str, dict[str, str]] = {}
        for section_name in _SECTION_NAMES:
            section = payload[section_name]
            if not isinstance(section, Mapping) or set(section) != {"headline", "body"}:
                raise ValueError("commentary section shape is invalid")
            headline = section["headline"]
            body = section["body"]
            if not _safe_text(headline, 240) or not _safe_text(body, 800):
                raise ValueError("commentary text is unsafe or outside bounds")
            if not _numbers_are_grounded(f"{headline} {body}", evidence_text):
                raise ValueError("commentary introduced a number outside the evidence")
            output[section_name] = {"headline": headline, "body": body}
        return output


def _safe_text(value: object, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and "<" not in value
        and ">" not in value
        and "javascript:" not in value.lower()
    )


def _numbers_are_grounded(text: str, evidence_text: str) -> bool:
    evidence_numbers = set(_NUMBER_PATTERN.findall(evidence_text))
    return set(_NUMBER_PATTERN.findall(text)).issubset(evidence_numbers)
