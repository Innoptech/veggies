"""Tests for scripts/molecule_matrix.py - the PR-scoped molecule matrix
generator (issue #77). Unit tests use synthetic fixtures; the repo-truth and
sentinel tests pin the real tree so a future coupling channel fails loudly
instead of silently mis-scoping the matrix."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import molecule_matrix

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/molecule_matrix.py"

# Tripwire: adding/renaming a role means updating this list deliberately.
ALL_ROLES = [
    "backup", "base", "crowdsec", "egress", "github_runner", "podman",
    "tailscale",
]

# Synthetic dep fixture: the egress and github_runner scenarios apply podman
# at converge time; every other scenario touches only its own role.
DEPS = {
    "egress": {"egress", "podman"},
    "github_runner": {"github_runner", "podman"},
}


def select(files):
    return molecule_matrix.select_roles(files, ALL_ROLES, DEPS)


def test_cli_and_agent_config_paths_affect_nothing():
    assert select(["cli/veggies.py", "agent-config/agents/cto.md"]) == []


def test_role_tasks_file_selects_its_role():
    assert select(["ansible/roles/tailscale/tasks/main.yml"]) == ["tailscale"]


def test_role_change_expands_through_converge_deps():
    assert select(["ansible/roles/podman/tasks/main.yml"]) == [
        "egress", "github_runner", "podman",
    ]


@pytest.mark.parametrize(
    "path",
    [
        "ansible/molecule/",  # the shared-input entry itself
        "ansible/molecule/fedora44-systemd.Containerfile",  # a file inside it
        "ansible/requirements.yml",
        "requirements-dev.txt",
        ".github/workflows/infra-ci.yml",
        "ansible.cfg",
    ],
)
def test_shared_inputs_select_everything(path):
    assert select([path]) == ALL_ROLES


def test_non_role_ansible_path_selects_everything():
    assert select(["ansible/playbooks/site.yml"]) == ALL_ROLES


def test_unknown_role_dir_selects_everything():
    assert select(["ansible/roles/no_such_role/x.yml"]) == ALL_ROLES


def test_git_quoted_path_selects_everything():
    # git C-style quotes filenames with unusual bytes; we cannot classify
    # them cleanly, so fail open.
    assert select(['"ansible/roles/base/we\\"ird.yml"']) == ALL_ROLES


def test_non_ansible_paths_affect_nothing():
    assert select(["secrets/crowdsec.yml", "docs/runbook.md", "README.md"]) == []


def test_multiple_role_paths_union_sorted():
    files = ["ansible/roles/base/x", "ansible/roles/backup/y"]
    assert select(files) == ["backup", "base"]


def test_empty_file_list_affects_nothing():
    assert select([]) == []


def test_role_dirs_matches_hardcoded_list():
    assert molecule_matrix.role_dirs(ROOT) == ALL_ROLES


def test_scenario_deps_from_real_tree():
    deps = molecule_matrix.scenario_deps(ROOT)
    assert {"egress", "podman"} <= deps["egress"]
    assert {"github_runner", "podman"} <= deps["github_runner"]
    for role in ALL_ROLES:
        assert role in deps[role], f"{role} must depend on itself"
    known = set(ALL_ROLES)
    for scenario, referenced in deps.items():
        assert referenced <= known, (
            f"{scenario} references unknown roles: {sorted(referenced - known)}"
        )


def test_no_include_or_import_role_anywhere():
    # converge.yml roles: lists are the ONLY cross-role coupling channel the
    # matrix generator can see; include_role/import_role would bypass it.
    for pattern in ("*.yml", "*.yaml"):
        for path in (ROOT / "ansible/roles").rglob(pattern):
            text = path.read_text()
            assert "include_role" not in text, path
            assert "import_role" not in text, path


def test_meta_main_declares_no_role_dependencies():
    for meta in (ROOT / "ansible/roles").glob("*/meta/main.yml"):
        data = yaml.safe_load(meta.read_text()) or {}
        assert not data.get("dependencies"), (
            f"{meta} declares role dependencies - an invisible coupling channel"
        )


def test_prepare_yml_never_applies_roles():
    for prepare in (ROOT / "ansible/roles").glob("*/molecule/*/prepare.yml"):
        for play in yaml.safe_load(prepare.read_text()) or []:
            assert "roles" not in play, f"{prepare} applies roles"


def test_no_relative_escape_into_sibling_role():
    for path in (ROOT / "ansible/roles").rglob("*.yml"):
        text = path.read_text()
        for role in ALL_ROLES:
            assert f"../{role}" not in text, f"{path} reaches into role {role}"


def run_cli(*args, stdin=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        input=stdin,
    )


def test_push_event_ignores_files_and_prints_full_matrix(tmp_path):
    files = tmp_path / "files.txt"
    files.write_text("cli/veggies.py\nagent-config/agents/cto.md\n")
    result = run_cli("--event-name", "push", "--files-file", str(files))
    assert result.returncode == 0
    assert result.stdout.strip() == json.dumps(ALL_ROLES)


def test_schedule_event_ignores_files_and_prints_full_matrix(tmp_path):
    files = tmp_path / "files.txt"
    files.write_text("cli/veggies.py\nagent-config/agents/cto.md\n")
    result = run_cli("--event-name", "schedule", "--files-file", str(files))
    assert result.returncode == 0
    assert result.stdout.strip() == json.dumps(ALL_ROLES)


def test_pull_request_reads_files_from_stdin():
    result = run_cli(
        "--event-name", "pull_request", "--files-file", "-",
        stdin="cli/veggies.py\n",
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "[]"


def test_pull_request_reads_files_from_file(tmp_path):
    files = tmp_path / "files.txt"
    files.write_text("ansible/roles/base/x\n")
    result = run_cli("--event-name", "pull_request", "--files-file", str(files))
    assert result.returncode == 0
    assert result.stdout.strip() == json.dumps(["base"])


def test_pull_request_requires_files_file():
    result = run_cli("--event-name", "pull_request")
    assert result.returncode == 2
    assert "files-file" in result.stderr


# --- dorny <-> script congruence -------------------------------------------
# The dorny `ansible` filter gates ansible-lint; select_roles gates the
# molecule matrix. Both must see the same ansible surface - a path that
# trips lint but maps to no scenario (or the reverse, beyond the documented
# exemption) is a coverage hole. Drift trips these tests in both directions.
EXPECTED_ANSIBLE_FILTER = {
    "ansible/**",
    "ansible.cfg",
    "requirements-dev.txt",
    ".ansible-lint",
    ".github/workflows/infra-ci.yml",
}


def _dorny_ansible_filter():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/infra-ci.yml").read_text()
    )
    filter_step = next(
        s
        for s in workflow["jobs"]["changes"]["steps"]
        if s.get("id") == "filter"
    )
    return set(yaml.safe_load(filter_step["with"]["filters"])["ansible"])


def select_real(files):
    return molecule_matrix.select_roles(
        files, molecule_matrix.role_dirs(ROOT), molecule_matrix.scenario_deps(ROOT)
    )


def test_dorny_ansible_filter_is_exactly_the_expected_set():
    assert _dorny_ansible_filter() == EXPECTED_ANSIBLE_FILTER


@pytest.mark.parametrize(
    "path",
    [
        "ansible/roles/base/tasks/main.yml",  # ansible/** role path
        "ansible.cfg",
        "requirements-dev.txt",
        ".github/workflows/infra-ci.yml",
    ],
)
def test_dorny_filter_paths_map_non_empty(path):
    assert select_real([path]) != []


def test_ansible_catch_all_selects_all_roles():
    # ansible/** also catches non-role ansible paths; those fail open.
    assert select_real(["ansible/playbooks/site.yml"]) == molecule_matrix.role_dirs(
        ROOT
    )


def test_ansible_lint_config_is_the_documented_exemption():
    # .ansible-lint trips the dorny filter (ansible-lint must rerun on lint
    # config changes) but maps to NO molecule scenario: molecule 26's default
    # test sequence has no lint step and no molecule.yml configures one -
    # scripts/molecule_matrix.py documents this exemption in select_roles.
    assert select_real([".ansible-lint"]) == []
