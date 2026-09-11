"""Pin-drift guard (issue #48, ADR 0046): the dev-tool binaries are pinned
by version AND sha256 in three places - the harness image
(deploy/images/opencode.Containerfile), the CI workflow env
(.github/workflows/infra-ci.yml), and `mask setup` (maskfile.md). All three
must agree, or a bump lands in one surface and breaks another three months
later. Also pins the converted hooks' shape: gitleaks/actionlint are
language: system under repo: local - hook time involves zero network
fetches (tofu-fmt precedent)."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent

# tool -> (version token, sha256 token) as they appear in each site
TOOLS = {
    "GITLEAK": ("8.30.1",
                "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"),
    "ACTIONLINT": ("1.7.12",
                   "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"),
    "TOFU": ("1.12.6",
             "5dc43da4f750f33873dc25e94587128709e819e544b7be9016b255316153c3a8"),
    "TFLINT": ("0.64.0",
               "cca9d13e2e1d7a2c627af60ff899a3c9b74212899416aeb96ec764d2ef954537"),
}

# tool -> asset filename in the `mask setup` curl URL (carries the version;
# the sha256 echo lines reference the bare download names instead)
ASSETS = {
    "GITLEAK": "gitleaks_8.30.1_linux_x64.tar.gz",
    "ACTIONLINT": "actionlint_1.7.12_linux_amd64.tar.gz",
    "TOFU": "tofu_1.12.6_linux_amd64.zip",
    "TFLINT": "tflint_linux_amd64.zip",
}


def _containerfile_pins():
    text = (ROOT / "deploy/images/opencode.Containerfile").read_text()
    return dict(re.findall(r"^ARG (\w+_(?:VERSION|SHA256))=(\S+)$", text, re.M))


def _workflow_pins():
    doc = yaml.safe_load((ROOT / ".github/workflows/infra-ci.yml").read_text())
    return doc["env"]


def _maskfile_text():
    return (ROOT / "maskfile.md").read_text()


def test_containerfile_carries_all_pins():
    args = _containerfile_pins()
    for tool, (version, sha) in TOOLS.items():
        assert args.get(f"{tool}_VERSION") == version, tool
        assert args.get(f"{tool}_SHA256") == sha, tool


def test_workflow_env_matches_containerfile():
    env = _workflow_pins()
    args = _containerfile_pins()
    for tool in TOOLS:
        for kind in ("VERSION", "SHA256"):
            key = f"{tool}_{kind}"
            assert str(env.get(key)) == args.get(key), key


def test_maskfile_setup_matches_containerfile():
    lines = _maskfile_text().splitlines()
    for tool, (version, sha) in TOOLS.items():
        asset = ASSETS[tool]
        # each tool's curl line names its asset (and carries the version in
        # the URL); the sha must sit on the echo line right after it -
        # swapping two tools' shas breaks this windowing
        for i, line in enumerate(lines[:-1]):
            if asset in line:
                assert version in line, f"{tool} version not on the curl line"
                assert sha in lines[i + 1], \
                    f"{tool} sha256 not on the line after the {asset} curl"
                break
        else:
            raise AssertionError(f"{tool} asset {asset} missing from maskfile")


def test_lint_hooks_are_local_system():
    doc = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    local_hooks = [h for repo in doc["repos"] if repo["repo"] == "local"
                   for h in repo["hooks"]]
    by_id = {h["id"]: h for h in local_hooks}
    for hook_id in ("gitleaks", "actionlint"):
        assert hook_id in by_id, f"{hook_id} hook missing"
        assert by_id[hook_id]["language"] == "system"
    # the converted entries mirror upstream v8.30.1 / v1.7.12 (ADR 0046)
    gitleaks = by_id["gitleaks"]
    assert gitleaks["entry"] == \
        "gitleaks git --pre-commit --redact --staged --verbose"
    assert gitleaks["pass_filenames"] is False
    actionlint = by_id["actionlint"]
    assert actionlint["entry"] == "actionlint"
    assert actionlint["files"] == "^\\.github/workflows/"
    assert actionlint["types"] == ["yaml"]
    # the network-fetching hook forms stay gone
    ids = [h["id"] for repo in doc["repos"] for h in repo["hooks"]]
    assert "actionlint-docker" not in ids
