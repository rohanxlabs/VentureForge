import os
import unittest
from pathlib import Path
from unittest.mock import patch

from src.database_config import CANONICAL_DATABASE_PATH, REPOSITORY_ROOT, resolve_database_path


class DatabaseConfigTests(unittest.TestCase):
    def test_canonical_path_is_the_unconfigured_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_database_path(), CANONICAL_DATABASE_PATH)

    def test_new_environment_override_precedes_legacy_override(self):
        with patch.dict(os.environ, {
            "VENTUREFORGE_DATABASE": "data/custom.sqlite",
            "VENTUREFORGE_PROJECT_DB": "data/legacy.sqlite",
        }, clear=True):
            self.assertEqual(resolve_database_path(), (REPOSITORY_ROOT / "data" / "custom.sqlite").resolve())

    def test_legacy_override_and_explicit_path_are_supported(self):
        with patch.dict(os.environ, {"VENTUREFORGE_PROJECT_DB": "legacy.sqlite"}, clear=True):
            self.assertEqual(resolve_database_path(), (REPOSITORY_ROOT / "legacy.sqlite").resolve())
        self.assertEqual(resolve_database_path(Path("alternate.sqlite")),
                         (REPOSITORY_ROOT / "alternate.sqlite").resolve())


if __name__ == "__main__":
    unittest.main()
