"""Tests for cli/garden.py - pure renderers, state, and drift guards."""

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("garden", ROOT / "cli/garden.py")
garden = importlib.util.module_from_spec(_spec)
sys.modules["garden"] = garden  # dataclass introspection needs this (py3.14)
_spec.loader.exec_module(garden)

INFRA_REPO = ROOT
FIXED_STATE = Path("/tmp/garden-test-state")


@pytest.fixture()
def spec(monkeypatch, tmp_path):
    monkeypatch.setenv("GARDEN_STATE_DIR", str(FIXED_STATE))
    repo = tmp_path / "demo-repo"
    repo.mkdir()
    return garden.StackSpec(name="demo", repo=str(repo), mode="mount", port=4096)


# --- names and ports -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/home/u/code/my-repo", "my-repo"),
        ("git@github.com:org/My_Repo.git", "my-repo"),
        ("https://github.com/org/foo.git/", "foo"),
        ("a__b  c", "a-b-c"),
    ],
)
def test_sanitize_name(raw, expected):
    assert garden.sanitize_name(raw) == expected


def test_sanitize_name_rejects_garbage():
    with pytest.raises(ValueError):
        garden.sanitize_name("!!!")


def test_allocate_port_first_free():
    assert garden.allocate_port(set()) == 4096
    assert garden.allocate_port({4096, 4097}) == 4098


# --- state ---------------------------------------------------------------------


def test_state_roundtrip_and_permissions(tmp_path):
    state = garden.State(root=tmp_path)
    state.add(garden.StackSpec(name="a", repo="/x", port=4096))
    state.add(garden.StackSpec(name="b", repo="/y", port=4097, host="garden"))
    data = state.load()
    assert set(data["stacks"]) == {"a", "b"}
    assert data["stacks"]["b"]["host"] == "garden"
    assert stat.S_IMODE(state.file.stat().st_mode) == 0o600
    assert state.used_ports() == {4096, 4097}
    assert state.remove("a") is True
    assert state.remove("a") is False


# --- pod rendering -------------------------------------------------------------


def _docs(spec):
    return list(yaml.safe_load_all(garden.render_yaml(spec, INFRA_REPO)))


def _pod(spec):
    return next(d for d in _docs(spec) if d["kind"] == "Pod")


def test_render_has_pvc_and_one_pod(spec):
    docs = _docs(spec)
    assert [d["kind"] for d in docs] == ["PersistentVolumeClaim", "Pod"]
    assert docs[0]["metadata"]["name"] == "garden-demo-opencode"
    # litellm is deliberately DB-less (ADR 0011: in-memory only); the proxy
    # dropped sqlite support, so no litellm-data volume exists.
    names = [v["name"] for v in docs[1]["spec"]["volumes"]]
    assert "litellm-data" not in names


def test_render_containers_and_pins(spec):
    containers = _pod(spec)["spec"]["containers"]
    by_name = {c["name"]: c for c in containers}
    assert set(by_name) == {"opencode", "litellm", "squid"}
    assert by_name["litellm"]["image"] == garden.IMAGE_LITELLM
    assert by_name["squid"]["image"] == garden.IMAGE_SQUID
    assert by_name["opencode"]["image"] == garden.IMAGE_OPENCODE


def test_only_opencode_publishes_a_port(spec):
    pod = _pod(spec)
    for container in pod["spec"]["containers"]:
        if container["name"] == "opencode":
            (port,) = container["ports"]
            assert port["containerPort"] == 4096
            assert port["hostPort"] == spec.port
            assert port["hostIP"] == "127.0.0.1"
        else:
            assert "ports" not in container


def test_remote_host_publishes_on_all_interfaces(spec):
    spec.host = "garden"
    (port,) = _pod(spec)["spec"]["containers"][0]["ports"]
    assert port["hostIP"] == "0.0.0.0"


def test_all_containers_hardened(spec):
    for container in _pod(spec)["spec"]["containers"]:
        sc = container["securityContext"]
        assert sc["readOnlyRootFilesystem"] is True
        assert sc["allowPrivilegeEscalation"] is False
        assert sc["capabilities"]["drop"] == ["ALL"]
        # squid must keep setuid/setgid to drop to the proxy user
        if container["name"] == "squid":
            assert sc["capabilities"]["add"] == ["SETUID", "SETGID"]
        assert container["resources"]["limits"]["memory"].endswith("Mi")
        assert "livenessProbe" in container


def test_no_subpath_mounts(spec):
    """subPath file mounts bypass SELinux relabeling (EACCES crash loop,
    2026-09-04). Directory mounts only, forever."""
    for container in _pod(spec)["spec"]["containers"]:
        for mount in container["volumeMounts"]:
            assert "subPath" not in mount, (container["name"], mount)


def test_secrets_are_namespaced_per_stack(spec):
    containers = _pod(spec)["spec"]["containers"]
    env = {e["name"]: e for c in containers for e in c.get("env", []) if "valueFrom" in e}
    assert env["FIREWORKS_API_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "garden-demo-litellm"
    assert env["OPENCODE_SERVER_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == "garden-demo-opencode"


def test_repo_is_the_only_code_mount(spec):
    pod = _pod(spec)
    volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
    assert volumes["repo"]["hostPath"]["path"] == spec.repo
    opencode = next(c for c in pod["spec"]["containers"] if c["name"] == "opencode")
    mounts = {m["name"]: m["mountPath"] for m in opencode["volumeMounts"]}
    assert mounts["repo"] == "/workspace"
    assert opencode["workingDir"] == "/workspace"


def test_opencode_json_stack_variant(spec):
    rendered = json.loads(garden.render_opencode_json(INFRA_REPO))
    litellm = rendered["provider"]["litellm"]
    assert litellm["options"]["baseURL"] == "http://127.0.0.1:4000/v1"
    assert litellm["options"]["apiKey"] == "{env:LITELLM_MASTER_KEY}"
    # everything else identical to the vendored config
    source = json.loads((INFRA_REPO / "agent-config/opencode.json").read_text())
    source["provider"]["litellm"]["options"] = litellm["options"]
    assert rendered == source


def test_squid_conf_matches_prod_shape():
    conf = garden.render_squid_conf()
    assert "http_access deny all" in conf
    assert "dstdomain" in conf
    allowlist = garden.render_allowlist().splitlines()
    assert "api.fireworks.ai" in allowlist
    assert allowlist[-1] == "api.fireworks.ai"  # model endpoints appended last


# --- drift guards against the Ansible side --------------------------------------


def test_litellm_pin_matches_role():
    defaults = yaml.safe_load(
        (ROOT / "ansible/roles/litellm/defaults/main.yml").read_text()
    )
    assert garden.IMAGE_LITELLM == defaults["litellm_image"]


def test_squid_allowlist_base_matches_role():
    defaults = yaml.safe_load(
        (ROOT / "ansible/roles/egress/defaults/main.yml").read_text()
    )
    assert garden.SQUID_ALLOWLIST_BASE == defaults["egress_allowlist_base"]


def test_model_endpoints_match_group_vars_example():
    text = (ROOT / "ansible/inventory/group_vars/all.yml.example").read_text()
    assert yaml.safe_load(text)["egress_model_endpoints"] == garden.SQUID_MODEL_ENDPOINTS


def test_squid_containerfile_reused():
    assert (ROOT / "ansible/roles/egress/files/squid.Containerfile").exists()


# --- runtime helpers (pure parts) -----------------------------------------------


def test_render_secret_docs_base64(spec):
    docs = garden.render_secret_docs(spec, {
        "master_key": "mk", "salt_key": "sk",
        "fireworks_api_key": "fw", "password": "pw",
    })
    assert {d["metadata"]["name"] for d in docs} == {
        "garden-demo-litellm", "garden-demo-opencode"
    }
    litellm = docs[0]["data"]
    assert litellm["master_key"] == "bWs="  # base64("mk")
    import base64
    assert base64.b64decode(litellm["fireworks_api_key"]).decode() == "fw"


def test_container_names_prefixed(spec):
    assert garden.container_names(spec) == [
        "garden-demo-opencode", "garden-demo-litellm", "garden-demo-squid"
    ]


def test_safe_rmtree_refuses_outside_paths(tmp_path):
    with pytest.raises(ValueError):
        garden.safe_rmtree(None, str(tmp_path), "/etc/someone-elses-repo")
    inside = tmp_path / "stack/config"
    inside.mkdir(parents=True)
    garden.safe_rmtree(None, str(tmp_path), str(inside))
    assert not inside.exists()


def test_state_records_password(tmp_path):
    state = garden.State(root=tmp_path)
    state.add(garden.StackSpec(name="a", repo="/x"), password="s3cret")
    assert state.get("a")["password"] == "s3cret"


def test_opencode_containerfile_pin_format():
    text = (ROOT / "deploy/images/opencode.Containerfile").read_text()
    assert "ghcr.io/anomalyco/opencode:1.18.27@sha256:" in text


# --- persistence (phase 12) -----------------------------------------------------


def test_quadlet_references_pod_yaml_only(spec, tmp_path):
    pod_yaml = tmp_path / "pod.yaml"
    text = garden.render_quadlet(spec, pod_yaml)
    assert f"Yaml={pod_yaml}" in text
    assert "WantedBy=default.target" in text
    assert "Secret" not in text


def test_rendered_yaml_never_contains_secrets(spec):
    """The on-disk pod.yaml (quadlet input) must never carry Secret docs."""
    assert "kind: Secret" not in garden.render_yaml(spec, INFRA_REPO)


def test_quadlet_path_honors_env(spec, monkeypatch, tmp_path):
    monkeypatch.setenv("GARDEN_QUADLET_DIR", str(tmp_path))
    assert garden.quadlet_path(spec) == f"{tmp_path}/garden-demo.kube"


# --- remote mode (ADR 0014) ------------------------------------------------------


def test_remote_spec_paths(spec):
    spec.host = "garden"
    assert spec.config_dir() == "/home/stacks/.local/state/garden/demo/config"
    assert spec.pod_yaml_path() == "/home/stacks/.local/state/garden/demo/pod.yaml"
    pod = _pod(spec)
    volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
    # remote stacks mount the shipped config copy, not an infra checkout
    assert volumes["agent-config"]["hostPath"]["path"] == spec.config_dir()
    assert volumes["stack-config"]["hostPath"]["path"] == spec.config_dir()


def test_remote_render_has_no_local_paths(spec):
    spec.host = "garden"
    text = garden.render_yaml(spec, INFRA_REPO)
    assert str(INFRA_REPO) not in text
    assert "/home/stacks/" in text


def test_host_run_wraps_ssh_sudo(monkeypatch):
    calls = []

    class R:
        returncode = 0
        stdout = ""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return R()

    monkeypatch.setattr(garden, "run", fake_run)
    garden.host_run(None, ["true"])
    garden.host_run("garden", ["podman", "pod", "ps"])
    assert calls[0] == ["true"]
    assert calls[1][:5] == ["ssh", "garden", "sudo", "-n", "-iu"]
    assert "stacks" in calls[1]


def test_stack_url_local_and_remote():
    assert garden.stack_url({"host": None, "port": 4096}) == "http://127.0.0.1:4096"
    assert garden.stack_url({"host": "garden", "port": 4097}) == "http://garden:4097"


def test_watchdog_units_are_minimal_and_scoped():
    assert 'label=app=garden' in garden.WATCHDOG_SERVICE
    assert 'status=exited' in garden.WATCHDOG_SERVICE
    assert "OnUnitActiveSec" in garden.WATCHDOG_TIMER
    assert "WantedBy=timers.target" in garden.WATCHDOG_TIMER


# --- golden file ----------------------------------------------------------------


def test_render_matches_golden(monkeypatch):
    monkeypatch.setenv("GARDEN_STATE_DIR", str(FIXED_STATE))
    fixed = garden.StackSpec(
        name="demo", repo="/tmp/garden-test-state/demo-repo", mode="mount", port=4096
    )
    golden = (ROOT / "tests/golden/pod.yaml").read_text()
    assert garden.render_yaml(fixed, INFRA_REPO) == golden
