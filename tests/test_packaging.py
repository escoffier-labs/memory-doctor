from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_project_version_is_sourced_from_package_module():
    config = (ROOT / "pyproject.toml").read_text()
    project = config.split("[project]", 1)[1].split("[project.urls]", 1)[0]

    assert '\nversion = "' not in project
    assert 'dynamic = ["version"]' in project
    assert "[tool.hatch.version]\npath = \"src/memory_doctor/__init__.py\"" in config
