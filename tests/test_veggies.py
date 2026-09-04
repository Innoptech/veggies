"""Tests for cli/veggies.py - pure renderers, state, and drift guards."""

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("veggies", ROOT / "cli/veggies.py")
veggies = importlib.util.module_from_spec(_spec)
sys.path.insert(0, str(ROOT / "cli"))
import veggies_stack  # noqa: E402
sys.modules["veggies"] = veggies  # dataclass introspection needs this (py3.14)
_spec.loader.exec_module(veggies)

INFRA_REPO = ROOT
FIXED_STATE = Path("/tmp/veggies-test-state")


@pytest.fixture()
def spec(monkeypatch, tmp_path):
    monkeypatch.setenv("VEGGIES_STATE_DIR", str(FIXED_STATE))
    repo = tmp_path / "demo-repo"
    repo.mkdir()
    return veggies.StackSpec(name="demo", repo=str(repo), mode="mount", port=4096)


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
    assert veggies.sanitize_name(raw) == expected


def test_sanitize_name_rejects_garbage():
    with pytest.raises(ValueError):
        veggies.sanitize_name("!!!")


def test_allocate_port_first_free():
    assert veggies.allocate_port(set()) == 4096
    assert veggies.allocate_port({4096, 4097}) == 4098


# --- state ---------------------------------------------------------------------


def test_state_roundtrip_and_permissions(tmp_path):
    state = veggies.State(root=tmp_path)
    state.add(veggies.StackSpec(name="a", repo="/x", port=4096))
    state.add(veggies.StackSpec(name="b", repo="/y", port=4097, host="veggies"))
    data = state.load()
    assert set(data["stacks"]) == {"a", "b"}
    assert data["stacks"]["b"]["host"] == "veggies"
    assert stat.S_IMODE(state.file.stat().st_mode) == 0o600
    assert state.used_ports() == {4096, 4097}
    assert state.remove("a") is True
    assert state.remove("a") is False


# --- pod rendering -------------------------------------------------------------


def _docs(spec):
    return list(yaml.safe_load_all(veggies.render_yaml(spec, INFRA_REPO)))


def _pod(spec):
    return next(d for d in _docs(spec) if d["kind"] == "Pod")


def test_render_has_pvc_and_one_pod(spec):
    docs = _docs(spec)
    assert [d["kind"] for d in docs] == ["PersistentVolumeClaim", "Pod"]
    assert docs[0]["metadata"]["name"] == "veggies-demo-opencode"
    # litellm is deliberately DB-less (ADR 0011: in-memory only); the proxy
    # dropped sqlite support, so no litellm-data volume exists.
    names = [v["name"] for v in docs[1]["spec"]["volumes"]]
    assert "litellm-data" not in names


def test_render_containers_and_pins(spec):
    containers = _pod(spec)["spec"]["containers"]
    by_name = {c["name"]: c for c in containers}
    assert set(by_name) == {"opencode", "litellm", "squid"}
    assert by_name["litellm"]["image"] == veggies.IMAGE_LITELLM
    assert by_name["squid"]["image"] == veggies.IMAGE_SQUID
    assert by_name["opencode"]["image"] == veggies.IMAGE_OPENCODE


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
    spec.host = "veggies"
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
    assert env["FIREWORKS_API_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "veggies-demo-litellm"
    assert env["OPENCODE_SERVER_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == "veggies-demo-opencode"


def test_repo_is_the_only_code_mount(spec):
    pod = _pod(spec)
    volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
    assert volumes["repo"]["hostPath"]["path"] == spec.repo
    opencode = next(c for c in pod["spec"]["containers"] if c["name"] == "opencode")
    mounts = {m["name"]: m["mountPath"] for m in opencode["volumeMounts"]}
    assert mounts["repo"] == "/workspace"
    assert opencode["workingDir"] == "/workspace"


def test_opencode_json_stack_variant(spec):
    rendered = json.loads(veggies.render_opencode_json(INFRA_REPO, "http://127.0.0.1:4000/v1"))
    litellm = rendered["provider"]["litellm"]
    assert litellm["options"]["baseURL"] == "http://127.0.0.1:4000/v1"
    assert litellm["options"]["apiKey"] == "{env:LITELLM_MASTER_KEY}"
    # everything else identical to the vendored config
    source = json.loads((INFRA_REPO / "agent-config/opencode.json").read_text())
    source["provider"]["litellm"]["options"] = litellm["options"]
    assert rendered == source


def test_squid_conf_matches_prod_shape():
    conf = veggies.render_squid_conf()
    assert "http_access deny all" in conf
    assert "dstdomain" in conf
    allowlist = veggies.render_allowlist().splitlines()
    assert "api.fireworks.ai" in allowlist
    assert allowlist[-1] == "api.fireworks.ai"  # model endpoints appended last


# --- drift guards against the Ansible side --------------------------------------


def test_squid_allowlist_base_matches_role():
    defaults = yaml.safe_load(
        (ROOT / "ansible/roles/egress/defaults/main.yml").read_text()
    )
    assert veggies.SQUID_ALLOWLIST_BASE == defaults["egress_allowlist_base"]


def test_model_endpoints_match_group_vars_example():
    text = (ROOT / "ansible/inventory/group_vars/all.yml.example").read_text()
    assert yaml.safe_load(text)["egress_model_endpoints"] == veggies.SQUID_MODEL_ENDPOINTS


def test_squid_containerfile_reused():
    assert (ROOT / "ansible/roles/egress/files/squid.Containerfile").exists()


# --- runtime helpers (pure parts) -----------------------------------------------


def test_render_secret_docs_base64(spec):
    docs = veggies.render_secret_docs(spec, {
        "master_key": "mk", "salt_key": "sk",
        "fireworks_api_key": "fw", "password": "pw",
    })
    assert {d["metadata"]["name"] for d in docs} == {
        "veggies-demo-litellm", "veggies-demo-opencode"
    }
    litellm = docs[0]["data"]
    assert litellm["master_key"] == "bWs="  # base64("mk")
    import base64
    assert base64.b64decode(litellm["fireworks_api_key"]).decode() == "fw"


def test_container_names_prefixed(spec):
    assert veggies.container_names(spec) == [
        "veggies-demo-opencode", "veggies-demo-litellm", "veggies-demo-squid"
    ]


def test_safe_rmtree_refuses_outside_paths(tmp_path):
    with pytest.raises(ValueError):
        veggies.safe_rmtree(None, str(tmp_path), "/etc/someone-elses-repo")
    inside = tmp_path / "stack/config"
    inside.mkdir(parents=True)
    veggies.safe_rmtree(None, str(tmp_path), str(inside))
    assert not inside.exists()


def test_state_records_password(tmp_path):
    state = veggies.State(root=tmp_path)
    state.add(veggies.StackSpec(name="a", repo="/x"), password="s3cret")
    assert state.get("a")["password"] == "s3cret"


def test_opencode_containerfile_pin_format():
    text = (ROOT / "deploy/images/opencode.Containerfile").read_text()
    assert "ghcr.io/anomalyco/opencode:1.18.27@sha256:" in text


# --- persistence (phase 12) -----------------------------------------------------


def test_quadlet_references_pod_yaml_only(spec, tmp_path):
    pod_yaml = tmp_path / "pod.yaml"
    text = veggies.render_quadlet(spec, pod_yaml)
    assert f"Yaml={pod_yaml}" in text
    assert "WantedBy=default.target" in text
    assert "Secret" not in text


def test_rendered_yaml_never_contains_secrets(spec):
    """The on-disk pod.yaml (quadlet input) must never carry Secret docs."""
    assert "kind: Secret" not in veggies.render_yaml(spec, INFRA_REPO)


def test_quadlet_path_honors_env(spec, monkeypatch, tmp_path):
    monkeypatch.setenv("VEGGIES_QUADLET_DIR", str(tmp_path))
    assert veggies.quadlet_path(spec) == f"{tmp_path}/veggies-demo.kube"


def test_core_registry_is_the_default_stack():
    assert [c.name for c in veggies_stack.CORE] == ["opencode", "litellm", "squid"]
    assert veggies_stack.COMPONENT_NAMES == {"opencode", "litellm", "squid"}


def test_render_pod_composes_components(spec):
    docs = veggies.render_yaml(spec, INFRA_REPO)
    subset = [c for c in veggies_stack.CORE if c.name != "opencode"]
    no_harness = veggies_stack.render_pod(spec, INFRA_REPO, components=subset)
    pod = [d for d in no_harness if d["kind"] == "Pod"][0]
    assert [c["name"] for c in pod["spec"]["containers"]] == ["litellm", "squid"]
    vol_names = {v["name"] for v in pod["spec"]["volumes"]}
    assert "opencode-home" not in vol_names  # owned by the opencode component
    assert docs  # full render unchanged


def test_requires_validation(spec):
    # opencode requires model-router + egress; without squid the stack is
    # incomplete and the error says so (ADR 0023).
    lonely = [c for c in veggies_stack.CORE if c.name == "opencode"]
    with pytest.raises(ValueError, match="requires 'model-router'"):
        veggies_stack.render_pod(spec, INFRA_REPO, components=lonely)


def test_parse_repo_config_schema_v0():
    cfg, warnings = veggies_stack.parse_repo_config("model: kimi-k3\ncomponents: [opencode, litellm, squid]\n")
    assert cfg == {"model": "kimi-k3", "components": ["opencode", "litellm", "squid"]}
    assert warnings == []


def test_parse_repo_config_unknown_key_warns():
    cfg, warnings = veggies_stack.parse_repo_config("mcps: [filesystem]\n")
    assert cfg == {}
    assert len(warnings) == 1 and "mcps" in warnings[0]


def test_parse_repo_config_bad_values_raise():
    with pytest.raises(ValueError, match="unknown components"):
        veggies_stack.parse_repo_config("components: [opencode, bogus]\n")
    with pytest.raises(ValueError, match="'model' must be a string"):
        veggies_stack.parse_repo_config("model: 42\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        veggies_stack.parse_repo_config("- just\n- a\n- list\n")


def test_load_repo_config_missing_is_empty(tmp_path):
    assert veggies_stack.load_repo_config(tmp_path) == ({}, [])


def test_model_override_lands_in_opencode_json():
    out = json.loads(veggies.render_opencode_json(INFRA_REPO, "http://127.0.0.1:4000/v1", model="devstral"))
    assert out["model"] == "litellm/devstral"
    default = json.loads(veggies.render_opencode_json(INFRA_REPO, "http://127.0.0.1:4000/v1"))
    assert "model" in default  # vendored default untouched when no override


def test_spec_components_flow_into_render(spec):
    spec.components = ["litellm", "squid"]  # requirement-complete subset
    pod = [d for d in veggies_stack.render_pod(spec, INFRA_REPO) if d["kind"] == "Pod"][0]
    assert [c["name"] for c in pod["spec"]["containers"]] == ["litellm", "squid"]
    with pytest.raises(ValueError, match="unknown components"):
        veggies_stack.render_pod(
            veggies.StackSpec(name="x", repo="/r", components=["bogus"]), INFRA_REPO)


def test_format_status_api_up():
    record = {"repo": "/r/demo", "mode": "mount", "port": 4096, "host": None}
    out = veggies.format_status(
        "demo", record,
        [("veggies-demo-opencode", "running", "healthy"),
         ("veggies-demo-litellm", "running", "healthy")],
        {"model": "litellm/kimi-k3", "sessions": [1, 2], "agents": ["a"] * 11,
         "busy": False})
    assert "model litellm/kimi-k3" in out
    assert "11 agents" in out and "2 sessions" in out and "BUSY" not in out
    assert "veggies-demo-litellm" in out


def test_format_status_api_down_and_busy():
    record = {"repo": "/r/demo", "mode": "clone", "port": 4097, "host": "veggies"}
    down = veggies.format_status("demo", record, [("veggies-demo-opencode", "exited", "-")], None)
    assert "unreachable" in down
    busy = veggies.format_status("demo", record, [],
                                 {"model": "m", "sessions": [], "agents": [], "busy": True})
    assert "BUSY" in busy


def test_probe_api_remote_uses_ssh_curl(monkeypatch):
    calls = []
    class R:
        returncode = 0
        stdout = '{"healthy": true}'
    monkeypatch.setattr(veggies, "run", lambda cmd, **kw: (calls.append(cmd), R())[1])
    out = veggies.probe_api("veggies", 4096, "pw", "/global/health")
    assert out == {"healthy": True}
    assert calls[0][:2] == ["ssh", "veggies"] and "curl" in calls[0]
    assert "opencode:pw" in calls[0]


def test_probe_api_failure_returns_none(monkeypatch):
    class R:
        returncode = 1
        stdout = ""
    monkeypatch.setattr(veggies, "run", lambda cmd, **kw: R())
    assert veggies.probe_api("veggies", 4096, "pw", "/config") is None


def test_required_secret_values_generic(spec):
    sources = veggies.required_secret_values(spec)
    assert isinstance(sources["master_key"], veggies.VaultKey) is False
    assert isinstance(sources["master_key"], veggies.Generated)
    assert isinstance(sources["fireworks_api_key"], veggies.VaultKey)
    assert veggies.Generated(12).nbytes == 12


def test_secret_names_match_declarations(spec):
    assert veggies.secret_names(spec) == [
        "veggies-demo-opencode", "veggies-demo-litellm"] or \
        sorted(veggies.secret_names(spec)) == [
            "veggies-demo-litellm", "veggies-demo-opencode"]


def test_legacy_hint_only_when_old_without_new(monkeypatch, tmp_path, capsys):
    old = tmp_path / "garden"
    new = tmp_path / "veggies"
    monkeypatch.setattr(veggies, "LEGACY_STATE_DIR", old)
    monkeypatch.setenv("VEGGIES_STATE_DIR", str(new))
    old.mkdir()
    veggies.legacy_hint()
    assert "renamed" in capsys.readouterr().err
    new.mkdir()
    veggies.legacy_hint()
    assert capsys.readouterr().err == ""


# --- remote mode (ADR 0014) ------------------------------------------------------


def test_remote_spec_paths(spec):
    spec.host = "veggies"
    assert spec.config_dir() == "/home/stacks/.local/state/veggies/demo/config"
    assert spec.pod_yaml_path() == "/home/stacks/.local/state/veggies/demo/pod.yaml"
    pod = _pod(spec)
    volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
    # remote stacks mount the shipped config copy, not an infra checkout
    assert volumes["agent-config"]["hostPath"]["path"] == spec.config_dir()
    assert volumes["stack-config"]["hostPath"]["path"] == spec.config_dir()


def test_remote_render_has_no_local_paths(spec):
    spec.host = "veggies"
    text = veggies.render_yaml(spec, INFRA_REPO)
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

    monkeypatch.setattr(veggies, "run", fake_run)
    veggies.host_run(None, ["true"])
    veggies.host_run("veggies", ["podman", "pod", "ps"])
    assert calls[0] == ["true"]
    assert calls[1][:5] == ["ssh", "veggies", "sudo", "-n", "-iu"]
    assert "stacks" in calls[1]


def test_stack_url_local_and_remote():
    assert veggies.stack_url({"host": None, "port": 4096}) == "http://127.0.0.1:4096"
    assert veggies.stack_url({"host": "veggies", "port": 4097}) == "http://veggies:4097"


def test_watchdog_units_are_minimal_and_scoped():
    assert 'label=app=veggies' in veggies.WATCHDOG_SERVICE
    assert 'status=exited' in veggies.WATCHDOG_SERVICE
    assert "OnUnitActiveSec" in veggies.WATCHDOG_TIMER
    assert "WantedBy=timers.target" in veggies.WATCHDOG_TIMER


# --- golden file ----------------------------------------------------------------


def test_render_matches_golden(monkeypatch):
    monkeypatch.setenv("VEGGIES_STATE_DIR", str(FIXED_STATE))
    fixed = veggies.StackSpec(
        name="demo", repo="/tmp/veggies-test-state/demo-repo", mode="mount", port=4096
    )
    golden = (ROOT / "tests/golden/pod.yaml").read_text()
    assert veggies.render_yaml(fixed, INFRA_REPO) == golden
