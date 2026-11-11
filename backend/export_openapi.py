"""Regenerate the root YAML contract from the FastAPI models and descriptions."""

from pathlib import Path

import yaml

from app.main import create_app


def main():
    root = Path(__file__).resolve().parent.parent
    document = yaml.safe_dump(create_app().openapi(), allow_unicode=True, sort_keys=False)
    (root / "openapi.yaml").write_text(document, encoding="utf-8")


if __name__ == "__main__":
    main()
