"""compose.yaml, the env examples and the documentation agree with the code: services, volumes, variables, commands and job intervals."""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path
from typing import Any, cast

import yaml

from svu.cli import HELP
from svu.config import TEST_ENV_PREFIX, Settings
from svu.worker.app import build_jobs

ROOT = Path(__file__).resolve().parents[2]
SVU_NAME = re.compile(r"\bSVU_[A-Z0-9_]*[A-Z0-9]\b")
DOCS = [ROOT / "README.md", *sorted((ROOT / "document").glob("*.md"))]


@cache
def compose() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8")))


def mounts(service: dict[str, Any]) -> list[tuple[str, str]]:
    found = []
    for entry in service.get("volumes", []):
        source, target = entry.split(":")[:2]
        found.append((source, target))
    return found


def doc_table(path: Path, heading: str) -> list[list[str]]:
    """The rows of the first table after the heading, as stripped cell texts without the header."""
    text = path.read_text(encoding="utf-8")
    section = text.split(heading, 1)[1]
    rows = []
    for line in section.splitlines():
        if line.startswith("|"):
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
        elif rows:
            break
    return rows[2:]


def test_every_mount_is_a_declared_named_volume() -> None:
    declared = set(compose()["volumes"])
    used = set()
    for name, service in compose()["services"].items():
        for source, target in mounts(service):
            assert source in declared, f"{name} mounts {source!r}, which is not a named volume"
            assert target.startswith("/")
            used.add(source)
    assert used == declared


def test_only_postgres_publishes_a_port_and_only_on_loopback() -> None:
    services = compose()["services"]
    assert {name for name, s in services.items() if s.get("ports")} == {"postgres"}
    assert all(str(p).startswith("127.0.0.1:") for p in services["postgres"]["ports"])


def test_backups_are_mounted_where_the_default_backup_dir_points() -> None:
    target = str(Settings.model_fields["backup_dir"].default)
    for name in ("worker", "backup"):
        assert ("backups", target) in mounts(compose()["services"][name])


def test_the_worker_and_the_tunnel_wait_for_a_healthy_api() -> None:
    services = compose()["services"]
    assert services["worker"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert services["cloudflared"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert "healthcheck" in services["api"]


def test_files_only_name_real_settings() -> None:
    files = [ROOT / "compose.yaml", ROOT / ".github/workflows/ci.yml", *sorted(ROOT.glob(".env*.example")), *DOCS]
    known = Settings.known_variables()
    unknown = {f"{p.name}: {n}" for p in files for n in SVU_NAME.findall(p.read_text(encoding="utf-8")) if n not in known and not n.startswith(TEST_ENV_PREFIX)}
    assert sorted(unknown) == []


def test_env_example_lists_every_setting() -> None:
    listed = set(re.findall(r"^(SVU_[A-Z0-9_]+)=", (ROOT / ".env.example").read_text(encoding="utf-8"), re.MULTILINE))
    assert listed == set(Settings.known_variables())


def test_operations_guide_lists_every_setting() -> None:
    rows = doc_table(ROOT / "document/operations.md", "## 設定")
    assert {row[0].strip("`") for row in rows} == set(Settings.known_variables())


def test_operations_guide_lists_every_command() -> None:
    rows = doc_table(ROOT / "document/operations.md", "`svu` コマンドには次のサブコマンドがある。")
    documented = {row[0].strip("`").split()[1] for row in rows}
    assert documented == set(HELP)


def test_operations_guide_lists_every_service() -> None:
    rows = doc_table(ROOT / "document/operations.md", "## サービス")
    assert {row[0].strip("`") for row in rows} == set(compose()["services"])


def test_architecture_lists_every_job_with_its_interval() -> None:
    rows = doc_table(ROOT / "document/architecture.md", "## worker のジョブ")
    documented = {row[0].strip("`"): float(row[1]) for row in rows}
    jobs = {job.name: job.interval_seconds for job in build_jobs(cast(Any, _JobOwner()))}
    assert documented == jobs


class _JobOwner:
    """Stands in for a Worker: build_jobs only reads the bound tick methods."""

    def __getattr__(self, name: str) -> Any:
        async def tick() -> None:
            return None

        return tick


def test_docs_use_ascii_punctuation() -> None:
    # Ellipsis, em and en dashes, curly quotes and the katakana middle dot.
    forbidden = {chr(c) for c in (0x2026, 0x2014, 0x2013, 0x201C, 0x201D, 0x2018, 0x2019, 0x30FB)}
    for path in [*DOCS, ROOT / ".env.example", ROOT / ".env.cloudflared.example"]:
        text = path.read_text(encoding="utf-8")
        found = {f"U+{ord(char):04X}" for char in forbidden if char in text}
        assert not found, f"{path.name} uses {sorted(found)}"


def test_readme_lists_every_slash_command() -> None:
    from svu.bot.app import ALL_COMMANDS

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    names: list[str] = []
    for command in ALL_COMMANDS:
        children = getattr(command, "walk_commands", None)
        if children is None:
            names.append(f"/{command.qualified_name}")
            continue
        for child in children():
            if not hasattr(child, "walk_commands"):
                names.append(f"/{child.qualified_name}")
    missing = [name for name in names if f"`{name}" not in readme]
    assert missing == []
