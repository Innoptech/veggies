"""Issue #52: path-gated jobs must always report required contexts - a path-skipped required check hangs as "Pending" forever and blocks merge."""

import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/infra-ci.yml").read_text())
JOBS = WORKFLOW["jobs"]

# required_checks in terraform is the branch-protection contract; the workflow
# must satisfy it. Parse the literal list out of the tfvars example.
_tfvars = (ROOT / "terraform/terraform.tfvars.example").read_text()
REQUIRED_CHECKS = ast.literal_eval(
    re.search(r"required_checks\s*=\s*(\[[^\]]*\])", _tfvars).group(1)
)

ALWAYS_AFTER_CHANGES = ["tofu", "tflint", "ansible-lint", "pytest", "molecule"]


def _context_name(key, job):
    # The check context is the job's name: if set, else the job key.
    return job.get("name", key)


def _needs(job):
    # needs: changes parses to str; needs: [a, b] to list - normalize to a set.
    needs = job.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    return set(needs)


def test_required_contexts_have_jobs():
    assert REQUIRED_CHECKS, "required_checks parse from tfvars came back empty"
    contexts = {_context_name(key, job) for key, job in JOBS.items()}
    for check in REQUIRED_CHECKS:
        assert check in contexts, f"no job reports required context {check!r}"


def test_pre_commit_never_gated():
    job = JOBS["pre-commit"]
    assert "if" not in job, "pre-commit must never be gated (security hooks)"
    assert "needs" not in job, "pre-commit must never wait on other jobs"


def test_path_gated_jobs_always_run_after_changes():
    for name in ALWAYS_AFTER_CHANGES:
        job = JOBS[name]
        # Exact equality, not a substring: `always() && <condition>` still
        # job-level-skips the required context and wedges merges as Pending.
        assert job.get("if", "").strip() == "always()", (
            f"required context {name} must have job-level if exactly "
            "'always()' - never add conditions; see the header comment in "
            "infra-ci.yml"
        )
        assert "changes" in _needs(job), f"{name}: must need the changes job"


def test_molecule_roles_gate_and_aggregate():
    roles_if = JOBS["molecule-roles"].get("if", "")
    assert "outputs.ansible" in roles_if
    assert "pull_request" in roles_if
    aggregate_needs = _needs(JOBS["molecule"])
    assert {"changes", "molecule-roles"} <= aggregate_needs


def test_molecule_roles_matrix_matches_role_dirs():
    # The workflow's "keep in sync with ansible/roles/" comment is now
    # enforced here - matrix drift fails this test.
    matrix_roles = JOBS["molecule-roles"]["strategy"]["matrix"]["role"]
    dir_roles = [p.name for p in (ROOT / "ansible/roles").iterdir() if p.is_dir()]
    assert sorted(matrix_roles) == sorted(dir_roles)


def test_changes_job_uses_paths_filter():
    assert "changes" in JOBS, "no changes job: path gating is not implemented"
    assert "if" not in JOBS["changes"], (
        "change detection must always run - a gate on it must be a "
        "deliberate, reviewed act"
    )
    uses = [step.get("uses", "") for step in JOBS["changes"].get("steps", [])]
    assert any(u.startswith("dorny/paths-filter@") for u in uses), (
        "changes job must compute outputs via dorny/paths-filter"
    )


def test_changes_outputs_wiring_is_closed():
    # Renaming a filter or outputs key resolves to "" with no error and
    # permanently pins RUN_JOB=false on PRs - silent green. Close the loop:
    # references, outputs keys and dorny filter keys must be the same set.
    raw = (ROOT / ".github/workflows/infra-ci.yml").read_text()
    referenced = set(
        re.findall(r"needs\.changes\.outputs\.([A-Za-z_][A-Za-z0-9_]*)", raw)
    )
    assert referenced == {"ansible", "terraform", "pytest"}, (
        f"unexpected needs.changes.outputs.* references: {sorted(referenced)}"
    )
    outputs = JOBS["changes"]["outputs"]
    for key in referenced:
        assert key in outputs, (
            f"changes job has no output {key!r} but other jobs reference it"
        )
    for key, value in outputs.items():
        # The output value must pass through the SAME-named filter output -
        # `${{ steps.filter.outputs.tf }}` under a `terraform:` key is silent.
        expected = "${{ steps.filter.outputs." + key + " }}"
        assert value == expected, (
            f"changes.outputs.{key} must be exactly {expected!r}, got {value!r}"
        )
    filter_step = next(
        s for s in JOBS["changes"]["steps"] if s.get("id") == "filter"
    )
    filters = yaml.safe_load(filter_step["with"]["filters"])
    assert referenced == set(filters) == set(outputs), (
        "dorny filter keys, changes.outputs keys and "
        "needs.changes.outputs.* references drifted: "
        f"references={sorted(referenced)} filters={sorted(filters)} "
        f"outputs={sorted(outputs)}"
    )


# Issue #76: the molecule test image is built once per run by an image job
# and shared with the matrix via artifact - matrix legs load, never build.
# A second image one day (another distro base) extends IMAGE_BUILD_JOBS.
IMAGE_BUILD_JOBS = ["molecule-image"]
IMAGE_TAG = "localhost/fedora44-systemd:latest"
IMAGE_ARTIFACT = "fedora44-systemd"


def _run_steps(job):
    return [s.get("run", "") for s in job.get("steps", [])]


def test_molecule_image_built_once_and_shared():
    image_jobs = [JOBS[name] for name in IMAGE_BUILD_JOBS]
    roles = JOBS["molecule-roles"]

    # The image jobs and the matrix share one gate, string-equal: divergence
    # either wastes a build per ansible-unchanged PR or reds seven legs on a
    # missing artifact.
    for name in IMAGE_BUILD_JOBS:
        assert JOBS[name].get("if", "").strip() == roles.get("if", "").strip(), (
            f"{name} gate must equal molecule-roles gate"
        )
        assert "changes" in _needs(JOBS[name])

    # The matrix waits on every image job; the aggregate sees them too.
    assert set(IMAGE_BUILD_JOBS) <= _needs(roles)
    assert set(IMAGE_BUILD_JOBS) <= _needs(JOBS["molecule"])

    # Matrix legs never build; builds live only in image jobs.
    for step_run in _run_steps(roles):
        assert "podman build" not in step_run
    for key, job in JOBS.items():
        builds = [r for r in _run_steps(job) if "podman build" in r]
        if key in IMAGE_BUILD_JOBS:
            assert len(builds) == 1, f"{key} must hold the single image build"
            assert "ansible/molecule/fedora44-systemd.Containerfile" in builds[0]
            assert f"-t {IMAGE_TAG}" in builds[0]
        else:
            assert not builds, f"{key} must not podman build"

    # The artifact contract: same name on upload and download, save/load tar
    # filenames agree, and every molecule scenario references the built tag.
    uploads = [
        s for name in IMAGE_BUILD_JOBS for s in JOBS[name].get("steps", [])
        if s.get("uses", "").startswith("actions/upload-artifact@")
    ]
    downloads = [
        s for s in roles.get("steps", [])
        if s.get("uses", "").startswith("actions/download-artifact@")
    ]
    assert len(uploads) == 1 and len(downloads) == 1
    assert uploads[0]["with"]["name"] == downloads[0]["with"]["name"] == IMAGE_ARTIFACT
    save = [r for r in _run_steps(JOBS[IMAGE_BUILD_JOBS[0]]) if "podman save" in r]
    load = [r for r in _run_steps(roles) if "podman load" in r]
    assert len(save) == 1 and len(load) == 1
    tar = re.search(r"(\S+\.tar)", save[0]).group(1)
    assert tar in load[0]

    molecule_yamls = sorted(ROOT.glob("ansible/roles/*/molecule/*/molecule.yml"))
    assert molecule_yamls, "no molecule scenarios found"
    for path in molecule_yamls:
        doc = yaml.safe_load(path.read_text())
        images = {p["image"] for p in doc["platforms"]}
        assert images == {IMAGE_TAG}, f"{path}: platforms must use {IMAGE_TAG}"
