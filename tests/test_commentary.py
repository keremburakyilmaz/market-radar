import json
import unittest
from pathlib import Path
from urllib.request import Request

from market_radar.commentary import ModelCommentaryEnhancer

ROOT = Path(__file__).resolve().parents[1]


def load_example_snapshot():
    with (ROOT / "examples" / "snapshot.v1.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def response_for(sections):
    return json.dumps(
        {"choices": [{"message": {"content": json.dumps(sections)}}]}
    ).encode("utf-8")


class ModelCommentaryEnhancerTests(unittest.TestCase):
    def test_rewrites_only_prose_and_preserves_deterministic_evidence(self):
        seen_request = None

        def requester(request: Request, timeout: float) -> bytes:
            nonlocal seen_request
            seen_request = request
            self.assertEqual(timeout, 45.0)
            return response_for(
                {
                    "dataRead": {
                        "headline": "Rates remain the main source of pressure",
                        "body": (
                            "Treasury yields lead the restrictive reading, while the positive "
                            "curve provides a partial offset."
                        ),
                    },
                    "newsRead": {
                        "headline": "Official observations anchor the news context",
                        "body": (
                            "The verified items explain the published inputs without turning "
                            "them into a forecast."
                        ),
                    },
                    "watchNext": {
                        "headline": "Inflation data is the next official test",
                        "body": (
                            "The next calendar release can reinforce or offset the rate pressure "
                            "already visible in the radar."
                        ),
                    },
                }
            )

        snapshot = load_example_snapshot()
        original_evidence = {
            name: snapshot["digest"]["commentary"][name]["evidenceIds"]
            for name in ("dataRead", "newsRead", "watchNext")
        }
        enhanced = ModelCommentaryEnhancer(
            api_key="test-key",
            model="test-model",
            requester=requester,
        ).enhance(snapshot)

        self.assertIsNot(enhanced, snapshot)
        self.assertEqual(
            enhanced["digest"]["commentary"]["generation"],
            {
                "mode": "model-assisted",
                "method": "daily-commentary-v1",
                "model": "test-model",
            },
        )
        for name, evidence_ids in original_evidence.items():
            self.assertEqual(
                enhanced["digest"]["commentary"][name]["evidenceIds"], evidence_ids
            )
        self.assertIsNotNone(seen_request)
        assert seen_request is not None
        self.assertEqual(
            seen_request.full_url,
            "https://integrate.api.nvidia.com/v1/chat/completions",
        )
        self.assertEqual(seen_request.get_header("Authorization"), "Bearer test-key")

    def test_falls_back_when_model_introduces_an_unsupported_number(self):
        def requester(_request: Request, _timeout: float) -> bytes:
            return response_for(
                {
                    "dataRead": {
                        "headline": "A fabricated threshold appeared",
                        "body": (
                            "The score should be compared with 999, which is not in the evidence."
                        ),
                    },
                    "newsRead": {"headline": "News", "body": "No unsupported claims."},
                    "watchNext": {"headline": "Watch", "body": "Use the official calendar."},
                }
            )

        snapshot = load_example_snapshot()
        enhanced = ModelCommentaryEnhancer(
            api_key="test-key",
            requester=requester,
        ).enhance(snapshot)

        self.assertIs(enhanced, snapshot)
        self.assertEqual(
            enhanced["digest"]["commentary"]["generation"]["mode"], "deterministic"
        )

    def test_rejects_an_insecure_model_endpoint(self):
        with self.assertRaisesRegex(ValueError, "credential-free HTTPS"):
            ModelCommentaryEnhancer(
                api_key="test-key",
                base_url="http://example.com/v1",
            )


if __name__ == "__main__":
    unittest.main()
