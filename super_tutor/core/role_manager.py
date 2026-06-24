"""Role system-prompt loader and context builder."""

import logging
from pathlib import Path

from super_tutor.core.exceptions import ConfigurationError, VALID_ROLES

logger = logging.getLogger(__name__)

# Constitution summary character limit to avoid blowing the context window.
_CONSTITUTION_MAX_CHARS = 6000


class RoleManager:
    """Loads role system-prompt templates and builds execution context.

    Templates are cached in memory after the first read to avoid repeated
    file-system access.

    Usage::

        mgr = RoleManager(prompts_dir="super_tutor/prompts")
        system_prompt = mgr.build_context(
            role="assistant",
            project_path="/home/user/sessions/demo",
            extra_context={"phase": "quiz_gen", "chunk_count": "12"},
        )
    """

    def __init__(self, prompts_dir: str) -> None:
        """Initialise the manager.

        Args:
            prompts_dir: Path to the directory containing role ``.md``
                template files.  Expected layout::

                    {prompts_dir}/tutor.md
                    {prompts_dir}/assistant.md
                    {prompts_dir}/evaluator.md
        """
        self._prompts_dir = Path(prompts_dir)
        self._cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_system_prompt(self, role: str) -> str:
        """Load the system-prompt template for *role*.

        Reads ``{prompts_dir}/{role}.md``.  Results are cached so
        subsequent calls for the same role do not touch disk.

        Args:
            role: One of ``"tutor"``, ``"assistant"``, ``"evaluator"``.

        Returns:
            The raw markdown template content.

        Raises:
            ConfigurationError: If *role* is not a recognised role name
                or if the template file does not exist.
        """
        if role not in VALID_ROLES:
            raise ConfigurationError(
                f"Unknown role '{role}'. Expected one of: {sorted(VALID_ROLES)}"
            )

        if role in self._cache:
            return self._cache[role]

        template_path = self._prompts_dir / f"{role}.md"
        if not template_path.is_file():
            raise ConfigurationError(f"Role template not found: {template_path}")

        content = template_path.read_text(encoding="utf-8")
        self._cache[role] = content
        logger.info("Loaded system prompt for role=%s from %s", role, template_path)
        return content

    # ------------------------------------------------------------------
    # Prompt version tracking
    # ------------------------------------------------------------------

    _VERSION_RE = __import__("re").compile(
        r"<!--\s*version:\s*(\S+)\s*\|\s*updated:\s*(\S+)\s*.*?-->"
    )

    @classmethod
    def _parse_version(cls, content: str) -> dict[str, str] | None:
        """从首行 ``<!-- version: ... -->`` 注释中提取版本信息。

        Returns:
            ``{"version": "1.0.0", "updated": "2026-06-24"}`` 或 ``None``。
        """
        first_line = content.split("\n", 1)[0]
        match = cls._VERSION_RE.search(first_line)
        if match:
            return {"version": match.group(1), "updated": match.group(2)}
        return None

    def get_all_versions(self) -> dict[str, dict[str, str] | None]:
        """返回所有已加载角色的 prompt 版本信息。

        Returns:
            ``{"tutor": {"version": "1.0.0", "updated": "2026-06-24"}, ...}``。
            未加载的角色值为 ``None``。
        """
        result: dict[str, dict[str, str] | None] = {}
        for role in VALID_ROLES:
            if role in self._cache:
                result[role] = self._parse_version(self._cache[role])
            else:
                result[role] = None
        return result

    def build_context(
        self,
        role: str,
        project_path: str,
        extra_context: dict[str, str] | None = None,
    ) -> str:
        """Build a complete system prompt by merging the role template with
        project-level context.

        The returned string is composed of three sections:

        1. The role's system-prompt template (see ``load_system_prompt``).
        2. A snapshot of the project constitution
           (``{project_path}/constitution/constitution.md``), if present.
        3. Key-value pairs from *extra_context* injected as a
           "运行时上下文" block.

        Args:
            role: Target role identifier.
            project_path: Root path of the active Super Tutor session directory.
            extra_context: Optional key-value pairs describing the current
                execution environment (e.g. ``{"current_module": "m2-llm-client",
                "token_budget_remaining": "15000"}``).

        Returns:
            The fully assembled system prompt string ready to be passed as
            the ``system`` message to an LLM.
        """
        parts: list[str] = []

        # 1. Core role template
        template = self.load_system_prompt(role)
        parts.append(template)

        # 2. Project constitution snapshot
        constitution = self._read_constitution(project_path)
        if constitution:
            parts.append("\n## 项目宪法摘要\n")
            parts.append(constitution)

        # 3. Runtime context
        if extra_context:
            parts.append("\n## 运行时上下文\n")
            for key, value in extra_context.items():
                parts.append(f"- **{key}**: {value}")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_constitution(project_path: str) -> str | None:
        """Read the project constitution and return its content.

        Returns ``None`` if the constitution file is missing or unreadable.
        Content is truncated to ``_CONSTITUTION_MAX_CHARS`` to avoid
        consuming excessive context window space.
        """
        constitution_path = Path(project_path) / "constitution" / "constitution.md"
        if not constitution_path.is_file():
            logger.debug("No constitution found at %s", constitution_path)
            return None

        try:
            text = constitution_path.read_text(encoding="utf-8")
            if len(text) > _CONSTITUTION_MAX_CHARS:
                text = text[:_CONSTITUTION_MAX_CHARS] + "\n\n... (宪法内容已截断)"
            return text
        except OSError as exc:
            logger.warning("Failed to read constitution at %s: %s", constitution_path, exc)
            return None
