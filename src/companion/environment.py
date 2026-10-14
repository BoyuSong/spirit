"""Optional local settings; exported/cloud variables take precedence."""
from pathlib import Path

from dotenv import load_dotenv


def load_environment(project_root: Path | None = None) -> bool:
    root = project_root if project_root is not None else Path(__file__).resolve().parents[2]
    return load_dotenv(root / ".env", override=False, interpolate=False, encoding="utf-8-sig")
