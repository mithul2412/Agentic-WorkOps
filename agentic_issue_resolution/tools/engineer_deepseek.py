from __future__ import annotations

import difflib
import json
import os
import re
from typing import Any

from agentic_issue_resolution.models.artifacts import EngineerPack, PatchArtifact
from agentic_issue_resolution.tools.llm_provider import GeminiDirectClient, LLMRequest


class GeminiEngineerClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        # Runtime is Gemini-direct only.
        self.api_key = (
            api_key
            or os.getenv("GEMINI_API_KEY", "")
            or os.getenv("GOOGLE_API_KEY", "")
        ).strip()
        self.base_url = (
            base_url
            or os.getenv("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta")
        ).rstrip("/")
        self.model = (
            model
            or os.getenv("ENGINEER_GEMINI_MODEL")
            or os.getenv("GEMINI_MODEL")
            or "gemini-1.5-pro"
        )

    def run_engineer(self, pack: EngineerPack) -> tuple[PatchArtifact, dict[str, Any]]:
        if self.api_key:
            try:
                patch = self._run_remote(pack)
                return patch, {"mode": "gemini_api", "model": self.model}
            except Exception as exc:  # noqa: BLE001
                fallback = self._deterministic_patch(pack)
                return fallback, {"mode": "deterministic_fallback", "reason": str(exc)}
        fallback = self._deterministic_patch(pack)
        return fallback, {"mode": "deterministic_fallback", "reason": "GEMINI_API_KEY missing"}

    def _run_remote(self, pack: EngineerPack) -> PatchArtifact:
        prompt_payload = {
            "ticket_id": pack.coding_brief.ticket_id,
            "risk_tier": pack.coding_brief.risk_tier.value,
            "summary": pack.coding_brief.summary,
            "hypothesis": pack.coding_brief.hypothesis,
            "acceptance_criteria": pack.coding_brief.acceptance_criteria,
            "allowed_files": pack.coding_brief.suspected_files,
            "files": pack.file_texts,
        }
        client = GeminiDirectClient(
            api_key=self.api_key,
            default_model=self.model,
            base_url=self.base_url,
            timeout_seconds=90,
        )
        response = client.chat(
            LLMRequest(
                system=(
                    "You are the Engineer Agent. "
                    "You must produce ONLY a unified diff patch. "
                    "Do not browse or search. Edit only allowed_files."
                ),
                user=(
                    "Generate a patch for this coding brief.\n"
                    "Return only unified diff text.\n"
                    f"{json.dumps(prompt_payload, ensure_ascii=False)}"
                ),
                temperature=0.0,
                max_tokens=700,
                expect_json=False,
            ),
            model=self.model,
        )
        content = response.text
        diff = self._extract_diff(content)
        return PatchArtifact(format="unified_diff", diff=diff)

    def _extract_diff(self, content: str) -> str:
        text = content.strip()
        fenced = re.findall(r"```(?:diff)?\n(.*?)```", text, flags=re.DOTALL)
        if fenced:
            text = fenced[0].strip()
        if not text.startswith("--- "):
            raise ValueError("engineer output did not return a valid unified diff")
        return text

    def _deterministic_patch(self, pack: EngineerPack) -> PatchArtifact:
        if not pack.coding_brief.suspected_files:
            raise ValueError("no target files available in coding brief")

        target = pack.coding_brief.suspected_files[0]
        original = pack.file_texts.get(target, "")
        comment_prefix = self._comment_prefix(target)
        marker_one = f"{comment_prefix} agentic_issue_resolution: prototype patch suggestion"
        marker_two = f"{comment_prefix} agentic_issue_resolution: add explicit validation guard"
        if marker_one not in original and marker_two not in original:
            prefix = "\n" if original and not original.endswith("\n") else ""
            updated = original + f"{prefix}{marker_one}\n{marker_two}\n"
        else:
            updated = original

        diff_lines = difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{target}",
            tofile=f"b/{target}",
            lineterm="",
        )
        diff = "\n".join(diff_lines).strip()
        if not diff:
            updated = original + ("\n" if original and not original.endswith("\n") else "") + f"{comment_prefix} patch noop\n"
            diff = "\n".join(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    updated.splitlines(keepends=True),
                    fromfile=f"a/{target}",
                    tofile=f"b/{target}",
                    lineterm="",
                )
            ).strip()
        return PatchArtifact(
            format="unified_diff",
            diff=diff,
            changed_files=[target],
        )

    def _comment_prefix(self, file_path: str) -> str:
        lower = file_path.lower()
        if lower.endswith((".js", ".ts", ".tsx", ".java", ".go", ".c", ".cpp", ".cs")):
            return "//"
        if lower.endswith((".html", ".xml")):
            return "<!--"
        if lower.endswith((".sql",)):
            return "--"
        return "#"


# Backward-compatible alias for older imports.
DeepSeekEngineerClient = GeminiEngineerClient
