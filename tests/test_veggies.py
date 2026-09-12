"""Tests for cli/veggies.py - pure renderers, state, and drift guards."""

import argparse
import base64
import importlib.util
import json
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("veggies", ROOT / "cli/veggies.py")
veggies = importlib.util.module_from_spec(_spec)
sys.path.insert(0, str(ROOT / "cli"))
import capabilities  # noqa: E402
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


def test_stack_name_from_dot_uses_cwd_basename(tmp_path, monkeypatch):
    repo_dir = tmp_path / "My Cool Repo"
    repo_dir.mkdir()
    monkeypatch.chdir(repo_dir)
    assert veggies.stack_name_from(".") == "my-cool-repo"


def test_stack_name_from_urls_pass_through():
    assert veggies.stack_name_from("https://github.com/org/foo.git") == "foo"
    assert veggies.stack_name_from("git@github.com:org/bar.git") == "bar"


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


def test_state_roundtrip_preserves_github(tmp_path, monkeypatch):
    monkeypatch.setenv("VEGGIES_STATE_DIR", str(tmp_path))
    spec = veggies.StackSpec(name="t", repo="/tmp/t", port=4096, github=True)
    veggies.State().add(spec, password="p")
    record = veggies.State().get("t")
    assert record["github"] is True
    assert veggies.spec_from_record("t", record).github is True


# --- pod rendering -------------------------------------------------------------


def _docs(spec):
    return list(yaml.safe_load_all(veggies.render_yaml(spec, INFRA_REPO)))


def _pod(spec):
    return next(d for d in _docs(spec) if d["kind"] == "Pod")


def test_render_has_pvc_and_one_pod(spec):
    docs = _docs(spec)
    assert [d["kind"] for d in docs] == ["PersistentVolumeClaim", "Pod"]
    assert docs[0]["metadata"]["name"] == "veggies-demo-opencode"
    # litellm is deliberately DB-less (in-memory only); the proxy
    # dropped sqlite support, so no litellm-data volume exists.
    names = [v["name"] for v in docs[1]["spec"]["volumes"]]
    assert "litellm-data" not in names


def test_render_containers_and_pins(spec):
    containers = _pod(spec)["spec"]["containers"]
    by_name = {c["name"]: c for c in containers}
    assert set(by_name) == {"opencode", "litellm", "squid"}
    assert by_name["litellm"]["image"] == veggies_stack.IMAGE_LITELLM
    assert by_name["squid"]["image"] == veggies_stack.IMAGE_SQUID
    assert by_name["opencode"]["image"] == veggies_stack.IMAGE_OPENCODE


def test_default_render_carries_no_ansible_vault_dummy(spec):
    # ADR 0053: the vault dummy is this-repo overlay glue, baked into the
    # image (ANSIBLE_VAULT_PASSWORD_FILE) - the shared renderer no longer
    # ships it to stacks whose repo has no ansible.
    args = _pod(spec)["spec"]["containers"][0]["args"][0]
    assert "vault-password" not in args
    assert ".config/infra" not in args


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


def test_litellm_stack_state_hostpath_mount(spec):
    # ADR 0051/0052: spend.jsonl lives at the stack state root - the whole
    # stack dir is the litellm container's only writable hostPath.
    pod = _pod(spec)
    litellm = next(c for c in pod["spec"]["containers"] if c["name"] == "litellm")
    mounts = {m["name"]: m for m in litellm["volumeMounts"]}
    assert mounts["stack-state"]["mountPath"] == "/stack-state"
    assert "readOnly" not in mounts["stack-state"]
    assert "costs" not in mounts
    env = {e["name"]: e.get("value") for e in litellm["env"]}
    assert env["VEGGIES_STACK"] == spec.name
    # custom_callbacks.py is imported from the readOnly /agent-config mount
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
    assert volumes["stack-state"]["hostPath"] == {
        "path": f"{spec.state_root()}/{spec.name}", "type": "Directory"}
    assert "costs" not in volumes
    names = [v["name"] for v in pod["spec"]["volumes"]]
    assert names.index("stack-state") < names.index("tmp")  # VOLUME_ORDER pinned


def test_litellm_config_files_ship_callbacks_remote_only(spec):
    import components.litellm as litellm
    # local: agent-config/litellm is live-mounted, nothing is copied
    assert litellm.COMPONENT.config_files(
        veggies_stack.build_context(spec, INFRA_REPO)) == {}
    spec.host = "vps"
    files = litellm.COMPONENT.config_files(
        veggies_stack.build_context(spec, INFRA_REPO))
    # remote: litellm loads custom_callbacks.py from the config file's dir,
    # so the callback ships next to the rendered config.yaml
    assert files["custom_callbacks.py"] == (
        INFRA_REPO / "agent-config/litellm/custom_callbacks.py").read_text()
    assert "config.yaml" in files


def test_opencode_json_stack_variant(spec):
    rendered = json.loads(veggies_stack.render_opencode_json(INFRA_REPO, "http://127.0.0.1:4000/v1"))
    litellm = rendered["provider"]["litellm"]
    assert litellm["options"]["baseURL"] == "http://127.0.0.1:4000/v1"
    assert litellm["options"]["apiKey"] == "{env:LITELLM_MASTER_KEY}"
    # everything else identical to the vendored config
    source = json.loads((INFRA_REPO / "agent-config/opencode.json").read_text())
    source["provider"]["litellm"]["options"] = litellm["options"]
    assert rendered == source


def test_opencode_config_files_ship_plugins(spec):
    import components.opencode as opencode
    files = opencode.COMPONENT.config_files(
        veggies_stack.build_context(spec, INFRA_REPO))
    # the metering plugin ships next to the vendored agents/skills so the
    # wrapper can copy it into opencode's global config dir (ADR 0052)
    assert files["plugins/metering.js"] == (
        INFRA_REPO / "agent-config/plugins/metering.js").read_text()
    assert any(k.startswith("agents/") for k in files)
    assert any(k.startswith("skills/") for k in files)


def test_opencode_wrapper_copies_plugins(spec):
    import components.opencode as opencode
    cont = opencode.COMPONENT.render(veggies_stack.build_context(spec, INFRA_REPO))
    assert "cp -r /stack-config/plugins /root/.config/opencode/ 2>/dev/null; " \
        in cont["args"][0]


def test_no_ask_anywhere():
    # ADR 0031 (scope amended by 0049): unattended sessions park forever on
    # `ask` - the merged envelope (vendored global config + agent frontmatter
    # + the repo's own project tier, which overrides global) is allow/deny
    # only, and `question` is denied explicitly (its default is ask).
    cfg = json.loads((INFRA_REPO / "agent-config/opencode.json").read_text())
    assert veggies_stack.ask_violations_in_config(
        cfg, "agent-config/opencode.json") == []
    assert cfg["permission"]["question"] == "deny"
    for agent in sorted((INFRA_REPO / "agent-config/agents").glob("*.md")):
        assert len(agent.read_text().split("---", 2)) == 3, \
            f"{agent.name}: missing frontmatter"
        assert veggies_stack.ask_violations_in_markdown(
            agent.read_text(), f"agent-config/agents/{agent.name}") == [], \
            f"{agent.name}: permission ask parks headless sessions"
    # The project tier of THIS repo (none today; live forever after - the
    # fixtures below prove the scan bites the moment one appears).
    assert veggies_stack.scan_project_tier(INFRA_REPO) == []


def test_project_tier_scan_flags_config_ask(tmp_path):
    (tmp_path / "opencode.json").write_text(
        json.dumps({"permission": {"edit": "ask"}}))
    violations = veggies_stack.scan_project_tier(tmp_path)
    assert any("opencode.json" in v and "permission.edit" in v
               for v in violations)


def test_project_tier_scan_flags_inline_agent_ask(tmp_path):
    (tmp_path / ".opencode").mkdir()
    (tmp_path / ".opencode/opencode.json").write_text(json.dumps(
        {"agent": {"reviewer": {"permission": {"bash": "ask"}}}}))
    violations = veggies_stack.scan_project_tier(tmp_path)
    assert any("agent.reviewer.permission.bash" in v for v in violations)


def test_project_tier_scan_flags_frontmatter_ask(tmp_path):
    d = tmp_path / ".claude/agents"
    d.mkdir(parents=True)
    (d / "evil.md").write_text(
        "---\nname: evil\npermission:\n  bash: ask\n---\nbody\n")
    violations = veggies_stack.scan_project_tier(tmp_path)
    assert any("evil.md" in v and "bash: ask" in v for v in violations)


def test_project_tier_scan_flags_quoted_permission_key(tmp_path):
    # Valid YAML: a quoted top-level key opens the block just the same -
    # the text scanner must not fail open on it.
    d = tmp_path / ".opencode/agents"
    d.mkdir(parents=True)
    (d / "q.md").write_text(
        '---\nname: q\n"permission":\n  bash: ask\n---\nbody\n')
    violations = veggies_stack.scan_project_tier(tmp_path)
    assert any("q.md" in v and "bash: ask" in v for v in violations)


def test_config_scan_skips_malformed_agent_section():
    # {"agent": []} is wrong-shaped but must not crash the scan - a
    # non-dict agent/mode section carries no permission tree, so skip it.
    assert veggies_stack.ask_violations_in_config({"agent": []}, "x") == []
    # Non-empty wrong shape proves the isinstance guard (empty [] slips
    # through `or {}` vacuously; a list with members raises AttributeError
    # on .items() without the guard).
    assert veggies_stack.ask_violations_in_config(
        {"agent": ["reviewer"]}, "x") == []
    assert veggies_stack.ask_violations_in_config(
        {"mode": "str"}, "x") == []


def test_project_tier_scan_ignores_comments_and_prose(tmp_path):
    # The vendored adversarial-review.md shape: the word "ask" inside a
    # frontmatter COMMENT or in body prose is not a permission value.
    d = tmp_path / ".agents/agents"
    d.mkdir(parents=True)
    (d / "ok.md").write_text(
        "---\nname: ok\npermission:\n"
        "  # no ask anywhere - asks park headless sessions\n"
        "  edit: deny\n  bash: allow\n---\nbody prose: ask away\n")
    (tmp_path / "opencode.jsonc").write_text(
        '{\n  // ask the user? never\n  "permission": {"edit": "deny"}\n}\n')
    assert veggies_stack.scan_project_tier(tmp_path) == []


def test_project_tier_scan_absent_and_clean(tmp_path):
    assert veggies_stack.project_tier_files(tmp_path) == []
    assert veggies_stack.scan_project_tier(tmp_path) == []


def test_project_tier_scan_fails_closed_on_unparseable(tmp_path):
    (tmp_path / "opencode.json").write_text('{"permission": ')
    violations = veggies_stack.scan_project_tier(tmp_path)
    assert any("cannot verify ask-free" in v for v in violations)


def test_project_tier_scan_keeps_violations_when_a_later_file_is_unreadable(tmp_path):
    # An ask found first must survive a later file's decode error: content
    # problems fail closed per file, they never discard what was found.
    (tmp_path / "opencode.json").write_text(
        json.dumps({"permission": {"edit": "ask"}}))
    d = tmp_path / ".claude/agents"
    d.mkdir(parents=True)
    (d / "bad.md").write_bytes(b"---\nname: bad\n---\n\xff\xfe body\n")
    violations = veggies_stack.scan_project_tier(tmp_path)
    assert any("permission.edit" in v for v in violations)
    assert any("bad.md" in v and "cannot verify ask-free" in v
               for v in violations)


def test_frontmatter_scan_tolerates_dashes_in_prose(tmp_path):
    # `---` inside frontmatter prose must not shift the scan window
    # (line-based fences, not a naive split).
    d = tmp_path / ".opencode/agents"
    d.mkdir(parents=True)
    (d / "p.md").write_text(
        "---\nname: p\ndescription: a --- b\npermission:\n"
        "  edit: ask\n---\nbody\n")
    violations = veggies_stack.scan_project_tier(tmp_path)
    assert any("p.md" in v and "edit: ask" in v for v in violations)


def test_project_tier_files_candidate_list(tmp_path):
    # Pins the candidate list against silent drift from upstream's walk set.
    for rel in ("opencode.json", "opencode.jsonc",
                ".opencode/opencode.json", ".opencode/opencode.jsonc",
                ".opencode/agents/a.md", ".claude/agents/b.md",
                ".agents/agent/c.md"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}" if "md" not in rel.rsplit(".", 1)[-1]
                     else "---\n---\n")
    found = {str(f.relative_to(tmp_path))
             for f in veggies_stack.project_tier_files(tmp_path)}
    assert found == {"opencode.json", "opencode.jsonc",
                     ".opencode/opencode.json", ".opencode/opencode.jsonc",
                     ".opencode/agents/a.md", ".claude/agents/b.md",
                     ".agents/agent/c.md"}


def test_squid_conf_matches_prod_shape():
    conf = veggies_stack.render_squid_conf()
    assert "http_access deny all" in conf
    assert "dstdomain" in conf
    # pod-loopback bypass: clients whose proxy libs ignore no_proxy
    # (aiohttp) must still reach in-pod services
    assert "acl pod_local dst 127.0.0.1" in conf
    assert "http_access allow pod_local" in conf
    allowlist = veggies_stack.render_allowlist().splitlines()
    assert "api.fireworks.ai" in allowlist
    assert allowlist[-1] == "api.fireworks.ai"  # model endpoints appended last


# --- drift guards against the Ansible side --------------------------------------


def test_squid_allowlist_base_matches_role():
    defaults = yaml.safe_load(
        (ROOT / "ansible/roles/egress/defaults/main.yml").read_text()
    )
    assert veggies_stack.SQUID_ALLOWLIST_BASE == defaults["egress_allowlist_base"]


def test_squid_allowlist_excludes_googleapis_storage():
    # ADR 0047: proxy.golang.org redirects module zips to signed
    # storage.googleapis.com URLs - the domain is all of GCS and stays OFF
    # the allowlist (baking pinned binaries was the chosen fix, issue #48).
    # This guards the in-repo BASE lists only - a live host could still add
    # the domain via egress_allowlist_extra in group_vars (out of repo by
    # design).
    assert "storage.googleapis.com" not in veggies_stack.SQUID_ALLOWLIST_BASE
    defaults = yaml.safe_load(
        (ROOT / "ansible/roles/egress/defaults/main.yml").read_text())
    assert "storage.googleapis.com" not in defaults["egress_allowlist_base"]


def test_squid_allowlist_covers_actions_log_upload():
    # GitHub Actions uploads job logs/artifacts to Azure results storage
    # (*.blob.core.windows.net - docs.github.com self-hosted-runner network
    # requirements); a deny loses all run logs (issue #25). The equality
    # drift test above forces the ansible role's allowlist to match.
    assert ".blob.core.windows.net" in veggies_stack.SQUID_ALLOWLIST_BASE


def test_model_endpoints_match_group_vars_example():
    text = (ROOT / "ansible/inventory/group_vars/all.yml.example").read_text()
    assert yaml.safe_load(text)["egress_model_endpoints"] == veggies_stack.SQUID_MODEL_ENDPOINTS


def test_squid_conf_chains_only_when_remote(spec):
    import components.squid as squid
    assert "cache_peer" not in squid.render_squid_conf(chained=False)
    chained = squid.render_squid_conf(chained=True)
    assert "cache_peer host.containers.internal parent 3128" in chained
    assert "never_direct allow all" in chained
    remote_spec = veggies_stack.StackSpec(name="x", repo="/r/x", host="veggies")
    files = squid.COMPONENT.config_files(
        veggies_stack.build_context(remote_spec, ROOT))
    assert "cache_peer" in files["squid.conf"]


def test_chained_squid_conf_fails_dns_fast():
    """Remote (chained) pods: the substrate drops this uid's direct egress,
    DNS included. Squid's own resolution of CONNECT targets stalls ~30s on
    the dead path before falling back to the parent (verified on veggies
    2026-09-10: 35s per new tunnel; parent answered in <1s). Fail DNS fast
    (nothing listens on loopback:53 -> instant refusal) - the parent
    resolves; never attempt direct for CONNECT; no netdb exchange noise."""
    import components.squid as squid
    conf = squid.render_squid_conf(chained=True)
    assert "dns_nameservers 127.0.0.1" in conf
    assert "nonhierarchical_direct off" in conf
    assert "no-netdb-exchange" in conf


def test_unchained_squid_conf_keeps_real_dns():
    import components.squid as squid
    conf = squid.render_squid_conf(chained=False)
    assert "dns_nameservers" not in conf
    assert "no-netdb-exchange" not in conf


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


def _image_tag(image: str) -> str:
    return image.rsplit(":", 1)[-1]


def test_opencode_base_containerfile_pin_format():
    # ADR 0053 pin chain: the base pins the UPSTREAM by tag+digest (the
    # digest pins the outside world). Its own tag must match
    # IMAGE_OPENCODE_BASE and both image constants stay in lockstep - a
    # partial bump would otherwise tag an overlay 1.x while silently
    # shipping an older opencode from the stale base.
    text = (ROOT / "deploy/images/opencode-base.Containerfile").read_text()
    from_lines = [l for l in text.splitlines() if l.startswith("FROM ")]
    assert len(from_lines) == 1  # multi-stage would tag the wrong stage
    upstream = (f"ghcr.io/anomalyco/opencode:"
                f"{_image_tag(veggies_stack.IMAGE_OPENCODE_BASE)}@sha256:")
    assert upstream in text
    assert _image_tag(veggies_stack.IMAGE_OPENCODE_BASE) == \
        _image_tag(veggies_stack.IMAGE_OPENCODE)


def test_opencode_overlay_is_from_pinned_base():
    # The overlay's FROM must be exactly the base image the component
    # builds (BuildSpec.base) - Containerfile/BuildSpec drift would only
    # surface as a failed remote build (the ea432ca class).
    text = (ROOT / "deploy/images/opencode.Containerfile").read_text()
    from_lines = [l for l in text.splitlines() if l.startswith("FROM ")]
    assert from_lines == [f"FROM {veggies_stack.IMAGE_OPENCODE_BASE}"]


# --- session worktrees (ADR 0037) ---------------------------------------------


def test_worktree_exclude_is_root_anchored():
    # Unanchored '.veggies/' would also swallow a nested tests/fixtures/
    # .veggies/; the worktree dir only ever exists at the repo root.
    assert veggies.WORKTREE_EXCLUDE == "/.veggies/"


def test_info_exclude_add_appends_and_is_idempotent():
    assert veggies.info_exclude_add("", "/.veggies/") == "/.veggies/\n"
    once = veggies.info_exclude_add("# comment\n*.pyc\n", "/.veggies/")
    assert once == "# comment\n*.pyc\n/.veggies/\n"
    assert veggies.info_exclude_add(once, "/.veggies/") == once


def test_info_exclude_add_matches_whole_lines():
    # '.veggies/' (unanchored) is a different pattern - it must not
    # satisfy a request for '/.veggies/'.
    assert veggies.info_exclude_add(".veggies/\n", "/.veggies/") == \
        ".veggies/\n/.veggies/\n"


def test_info_exclude_add_repairs_missing_trailing_newline():
    assert veggies.info_exclude_add("*.pyc", "/.veggies/") == \
        "*.pyc\n/.veggies/\n"


# --- persistence -----------------------------------------------------


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
    # incomplete and the error says so.
    lonely = [c for c in veggies_stack.CORE if c.name == "opencode"]
    with pytest.raises(ValueError, match="requires 'model-router'"):
        veggies_stack.render_pod(spec, INFRA_REPO, components=lonely)


def test_parse_repo_config_schema_v0():
    cfg, warnings = veggies_stack.parse_repo_config("model: kimi-k3\ncomponents: [opencode, litellm, squid]\n")
    assert cfg == {"model": "kimi-k3", "components": ["opencode", "litellm", "squid"]}
    assert warnings == []


def test_parse_repo_config_unknown_key_warns():
    cfg, warnings = veggies_stack.parse_repo_config("bogus: [filesystem]\n")
    assert cfg == {}
    assert len(warnings) == 1 and "bogus" in warnings[0]


def test_parse_repo_config_mcps():
    cfg, warnings = veggies_stack.parse_repo_config("mcps: [toolbox]\n")
    assert cfg == {"mcps": ("toolbox",)}
    assert warnings == []
    with pytest.raises(ValueError, match="unknown mcps"):
        veggies_stack.parse_repo_config("mcps: [bogus]\n")
    with pytest.raises(ValueError, match="'mcps' must be a list of strings"):
        veggies_stack.parse_repo_config("mcps: toolbox\n")


def test_parse_repo_config_github():
    cfg, _ = veggies_stack.parse_repo_config("github: true\n")
    assert cfg["github"] is True
    cfg, _ = veggies_stack.parse_repo_config("github: false\n")
    assert cfg["github"] is False
    cfg, _ = veggies_stack.parse_repo_config("mcps: [toolbox]\n")
    assert "github" not in cfg
    # quoted on purpose: a bare `yes` is a YAML 1.1 bool to PyYAML and would
    # PASS validation; the rejection path needs a genuine non-bool
    with pytest.raises(ValueError, match="'github' must be a bool"):
        veggies_stack.parse_repo_config('github: "yes"\n')


def test_stack_components_includes_mcps():
    spec = veggies_stack.StackSpec(name="t", repo="/tmp/x", mcps=("toolbox",))
    names = [c.name for c in veggies_stack.stack_components(spec)]
    assert names == ["opencode", "litellm", "squid", "toolbox"]


def test_toolbox_render_and_mcp_entry():
    spec = veggies_stack.StackSpec(name="t", repo="/tmp/x", mcps=("toolbox",))
    ctx = veggies_stack.build_context(spec, INFRA_REPO)
    tb = ctx.components[-1]
    container = tb.render(ctx)
    assert "ports" not in container  # pod loopback only, never published
    assert container["securityContext"] == veggies_stack.HARDENED
    entry = tb.mcp_entry(ctx)
    assert entry["type"] == "remote" and entry["url"].endswith(":7000/mcp")
    assert "mcp-toolbox-server.py" in tb.config_files(ctx)


def test_parse_repo_config_supervision():
    cfg, warnings = veggies_stack.parse_repo_config("supervision: supervisor\n")
    assert cfg == {"selections": {"supervision": "supervisor"}}
    assert warnings == []
    with pytest.raises(ValueError, match="unknown supervision implementation"):
        veggies_stack.parse_repo_config("supervision: bogus\n")
    with pytest.raises(ValueError, match="'supervision' must be a string"):
        veggies_stack.parse_repo_config("supervision: [x]\n")


def test_supervision_is_opt_in_and_order_stable(spec):
    # default stacks are untouched (the golden file proves the render)
    assert "supervisor" not in [c.name for c in veggies_stack.stack_components(spec)]
    spec.selections = {"supervision": "supervisor"}
    names = [c.name for c in veggies_stack.stack_components(spec)]
    assert names == ["opencode", "litellm", "squid", "supervisor"]
    # the v0 `components:` path can name it too
    by_name = veggies_stack.StackSpec(
        name="t", repo="/tmp/x",
        components=["opencode", "litellm", "squid", "supervisor"])
    assert [c.name for c in veggies_stack.stack_components(by_name)][-1] == "supervisor"
    # and it flows into the pod render + health wait
    pod = _pod(spec)
    assert [c["name"] for c in pod["spec"]["containers"]] == names
    assert veggies.container_names(spec)[-1] == "veggies-demo-supervisor"


def test_supervisor_component_render(spec):
    """The always-on critic sidecar (ADR 0036): loopback-only, hardened,
    zero egress, reusing existing pod secrets (never declaring its own)."""
    spec.selections = {"supervision": "supervisor"}
    ctx = veggies_stack.build_context(spec, INFRA_REPO)
    sup = ctx.components[-1]
    assert sup.provides == "supervision"
    container = sup.render(ctx)
    assert "ports" not in container  # pod loopback only, never published
    assert container["securityContext"] == veggies_stack.HARDENED
    assert sup.secrets(spec) == []  # reuses harness + router secrets
    by_name = {e["name"]: e for e in container["env"]}
    assert by_name["OPENCODE_SERVER_PASSWORD"]["valueFrom"]["secretKeyRef"] == \
        {"name": "veggies-demo-opencode", "key": "password"}
    assert by_name["LITELLM_MASTER_KEY"]["valueFrom"]["secretKeyRef"] == \
        {"name": "veggies-demo-litellm", "key": "master_key"}
    assert by_name["OPENCODE_URL"]["value"] == "http://127.0.0.1:4096"
    assert by_name["ROUTER_URL"]["value"] == "http://127.0.0.1:4000/v1"
    # zero egress by construction: no proxy env reaches the container
    assert not set(by_name) & {"HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"}
    # unbuffered logs: the pod log is the ONLY operator surface (PASS/STOP
    # are log-only by design), block-buffered prints would hide verdicts
    assert by_name["PYTHONUNBUFFERED"]["value"] == "1"
    # the heartbeat lives on a dedicated emptyDir the agent cannot write
    assert sup.volumes(ctx) == [{"name": "supervisor-tmp", "emptyDir": {}}]
    mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
    assert mounts["supervisor-tmp"] == "/tmp"
    files = sup.config_files(ctx)
    assert files["supervisor.py"] == (INFRA_REPO / "cli/supervisor.py").read_text()
    assert "supervise-daemon.py" in files
    (probe,) = sup.probes(spec)
    assert probe.label == "critic" and probe.kind == "exec"


def test_render_opencode_json_mcp_block():
    out = json.loads(veggies_stack.render_opencode_json(
        INFRA_REPO, "http://127.0.0.1:4000/v1",
        mcp_entries={"toolbox": {"type": "remote",
                                 "url": "http://127.0.0.1:7000/mcp"}}))
    assert out["mcp"]["toolbox"]["url"] == "http://127.0.0.1:7000/mcp"


def test_render_allowlist_merges_component_domains():
    allowlist = veggies_stack.render_allowlist(["example.com"]).splitlines()
    assert "example.com" in allowlist
    assert "github.com" in allowlist  # base entries survive the merge


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
    out = json.loads(veggies_stack.render_opencode_json(INFRA_REPO, "http://127.0.0.1:4000/v1", model="devstral"))
    assert out["model"] == "litellm/devstral"
    default = json.loads(veggies_stack.render_opencode_json(INFRA_REPO, "http://127.0.0.1:4000/v1"))
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
        ["model litellm/kimi-k3", "agents 11", "sessions 2", "activity idle"])
    assert "model litellm/kimi-k3" in out
    assert "agents 11" in out and "sessions 2" in out
    assert "veggies-demo-litellm" in out


def test_format_status_api_down():
    record = {"repo": "/r/demo", "mode": "clone", "port": 4097, "host": "veggies"}
    down = veggies.format_status("demo", record, [("veggies-demo-opencode", "exited", "-")], None)
    assert "unreachable" in down


def test_harness_probes_and_attach_contract(spec):
    harness = veggies.harness_of(spec)
    assert harness is not None and harness.name == "opencode"
    labels = [p.label for p in harness.probes(spec)]
    assert labels == ["model", "agents", "sessions", "activity"]
    argv = harness.attach("http://127.0.0.1:4096", "pw")
    assert argv[:2] == ["opencode", "attach"] and "pw" in argv


def test_stub_harness_proves_the_seam(spec, tmp_path):
    """A harness the CLI has never heard of assembles through the generic
    path: the seam, not the implementation list, is the contract."""
    stub = veggies_stack.Component(
        name="stub",
        provides="harness",
        requires=("model-router", "egress"),
        render=lambda ctx: {
            "name": "stub", "image": "localhost/stub:latest",
            "env": [{"name": k, "value": v}
                    for k, v in ctx.service("egress").env.items()]
            + [{"name": "ROUTER", "value": ctx.service("model-router").base_url}],
        },
        volumes=lambda ctx: [{"name": "tmp", "emptyDir": {}}],
    )
    docs = veggies_stack.render_pod(
        spec, INFRA_REPO,
        components=[stub, veggies_stack.REGISTRY["model-router"]["litellm"],
                    veggies_stack.REGISTRY["egress"]["squid"]])
    pod = [d for d in docs if d["kind"] == "Pod"][0]
    env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
    assert env["ROUTER"] == "http://127.0.0.1:4000/v1"
    assert env["http_proxy"] == "http://127.0.0.1:3128"
    # and its secrets come from declarations, not hardcoding
    assert veggies.render_secret_docs(spec, {"password": "x"}, components=[
        veggies_stack.Component("s", "harness", (), lambda c: {}, lambda c: [],
                                secrets=lambda sp: [
                                    veggies_stack.SecretSpec(
                                        "s", {"password": veggies_stack.Generated(8)})])
    ])[0]["metadata"]["name"] == "veggies-demo-s"


def test_component_build_descriptors(spec):
    # Images are component-owned: a stack builds/pulls exactly what it runs.
    builds = {c.name: c.build for c in veggies_stack.CORE}
    assert builds["opencode"].containerfile == "deploy/images/opencode.Containerfile"
    base = builds["opencode"].base
    assert base is not None
    assert base.image == veggies_stack.IMAGE_OPENCODE_BASE
    assert base.containerfile == "deploy/images/opencode-base.Containerfile"
    assert base.base is None  # single level only (BuildSpec docstring)
    assert builds["litellm"].base is None  # pull-only: no chain
    assert builds["squid"].containerfile.endswith("squid.Containerfile")
    assert builds["litellm"].containerfile is None  # pull-only
    subset = veggies_stack.resolve_components(names=["litellm", "squid"])
    assert [c.build.image for c in subset] == [
        veggies_stack.IMAGE_LITELLM, veggies_stack.IMAGE_SQUID]


def test_buildspec_base_is_single_level():
    base = capabilities.BuildSpec("localhost/g:1", "g.Containerfile")
    mid = capabilities.BuildSpec("localhost/m:1", "m.Containerfile", base=base)
    with pytest.raises(ValueError, match="single-level"):
        capabilities.BuildSpec("localhost/t:1", "t.Containerfile", base=mid)


def test_registry_capability_keys(spec):
    # v1 capability keys resolve
    comps = veggies_stack.resolve_components(selections={"harness": "opencode"})
    assert [c.name for c in comps] == ["opencode", "litellm", "squid"]
    with pytest.raises(ValueError, match="unknown harness implementation"):
        veggies_stack.resolve_components(selections={"harness": "claude-code"})
    # veggies.yml v1 keys
    cfg, _ = veggies_stack.parse_repo_config("harness: opencode\nmodel_router: litellm\n")
    assert cfg["selections"] == {"harness": "opencode", "model-router": "litellm"}
    with pytest.raises(ValueError, match="not both"):
        veggies_stack.parse_repo_config("components: [squid]\nharness: opencode\n")


def test_retired_control_plane_selection_is_dropped():
    # state.json from the canvas era (ADR 0028): the retired capability is
    # dropped with a warning instead of crashing on an unknown impl.
    spec = veggies.spec_from_record("old", {
        "repo": "/x", "host": None, "port": 4097, "mode": "clone",
        "selections": {"control-plane": "builtin"}})
    assert spec.selections is None


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
    assert isinstance(sources["master_key"], veggies_stack.VaultKey) is False
    assert isinstance(sources["master_key"], veggies.Generated)
    assert isinstance(sources["fireworks_api_key"], veggies_stack.VaultKey)
    assert veggies.Generated(12).nbytes == 12


def test_secret_names_match_declarations(spec):
    assert veggies.secret_names(spec) == [
        "veggies-demo-opencode", "veggies-demo-litellm"] or \
        sorted(veggies.secret_names(spec)) == [
            "veggies-demo-litellm", "veggies-demo-opencode"]


def test_vaultkey_defaults_to_model_vault():
    assert capabilities.VaultKey("fireworks_api_key").vault == "secrets/model.yml"


def test_stackspec_github_flag_and_secret_name():
    spec = veggies.StackSpec(name="t", repo="/tmp/t", port=4096)
    assert spec.github is False
    assert veggies.StackSpec(name="t", repo="/tmp/t", port=4096, github=True).github is True
    assert spec.secret_github == "veggies-t-github"


def test_github_optin_adds_secret_env_and_gitconfig():
    import components.opencode as opencode
    spec = veggies.StackSpec(name="demo", repo="/tmp/r", port=4096, github=True)
    secs = {s.name_suffix: s for s in opencode.COMPONENT.secrets(spec)}
    assert secs["github"].keys["token"] == capabilities.VaultKey(
        "github_token", "secrets/github.yml")
    # ADR 0033: the serve password comes from the vault on github stacks so
    # the repo's Actions secret (same vault key via tofu) never goes stale.
    assert secs["opencode"].keys["password"] == capabilities.VaultKey(
        "veggies_stack_password", "secrets/github.yml")
    ctx = veggies_stack.build_context(spec, INFRA_REPO)
    cont = opencode.COMPONENT.render(ctx)
    env = {e["name"]: e for e in cont["env"] if "name" in e}
    assert env["GH_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "veggies-demo-github", "key": "token"}
    args = cont["args"][0]
    # single-quoting is the security property: $GH_TOKEN must stay literal in
    # the git config and expand only when git invokes the helper - a
    # double-quoted helper would bake the PAT into /root/.gitconfig.
    assert "credential.helper '!f() {" in args and "$GH_TOKEN" in args
    assert "url.https://github.com/.insteadOf" in args
    assert 'user.name "veggies-agent"' in args


def test_github_default_off_leaves_render_untouched():
    import components.opencode as opencode
    spec = veggies.StackSpec(name="demo", repo="/tmp/r", port=4096)
    secs = {s.name_suffix: s for s in opencode.COMPONENT.secrets(spec)}
    assert "github" not in secs
    # non-github stacks keep the per-stack random serve password
    assert isinstance(secs["opencode"].keys["password"], capabilities.Generated)
    cont = opencode.COMPONENT.render(veggies_stack.build_context(spec, INFRA_REPO))
    blob = str(cont)
    assert "GH_TOKEN" not in blob and "credential.helper" not in blob


def test_resolve_secret_values_routes_each_key_to_its_vault(monkeypatch, spec):
    # The up-time seam cmd_up uses: a VaultKey's own vault file decides
    # which vault it is read from.
    stub = veggies_stack.Component(
        name="stub", provides="stub", requires=(),
        render=lambda ctx: {}, volumes=lambda ctx: [],
        secrets=lambda s: [capabilities.SecretSpec("github", {
            "token": capabilities.VaultKey("github_token", capabilities.VAULT_GITHUB),
            "model_key": capabilities.VaultKey("fireworks_api_key"),
        })],
    )
    calls = []
    monkeypatch.setattr(veggies, "vault_key",
                        lambda key, vault: calls.append((key, vault)) or "x")
    values = veggies.resolve_secret_values(spec, [stub])
    assert calls == [("github_token", "secrets/github.yml"),
                     ("fireworks_api_key", "secrets/model.yml")]
    assert values == {"token": "x", "model_key": "x"}


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_warm_api_retries_until_answer(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(veggies.time, "time", clock.time)
    monkeypatch.setattr(veggies.time, "sleep", clock.sleep)
    seq = iter([None, None, {"model": "m"}])
    monkeypatch.setattr(veggies, "probe_api", lambda *a, **k: next(seq))
    assert veggies.warm_api(None, 1, "p", timeout=30) is True
    monkeypatch.setattr(veggies, "probe_api", lambda *a, **k: None)
    assert veggies.warm_api(None, 1, "p", timeout=5) is False


def test_logs_remote_uses_env_wrap_not_login_shell(monkeypatch):
    # `sudo -iu stacks` re-parses through the nologin shell and dies with
    # "account is currently not available" (verified 2026-09-10).
    monkeypatch.setenv("VEGGIES_STATE_DIR", "/tmp/veggies-test-state")
    monkeypatch.setattr(veggies.State, "get", lambda self, n: {
        "repo": "/r", "mode": "clone", "port": 4098, "host": "veggies",
        "password": "p"})
    monkeypatch.setattr(veggies, "_remote_uid", lambda h: "1003")
    calls = []
    monkeypatch.setattr(veggies.os, "execvp",
                        lambda exe, argv: calls.append(argv))
    veggies.cmd_logs(argparse.Namespace(name="v", container="opencode",
                                        follow=True))
    argv = calls[0]
    assert argv[0:2] == ["ssh", "veggies"]
    payload = argv[2]
    assert "sudo -n -u stacks env HOME=/home/stacks" in payload
    assert "-i" not in payload.split("env")[0]
    assert shlex.split(payload)[-3:] == ["podman", "logs", "-f"] or \
        shlex.split(payload)[-4:] == ["podman", "logs", "-f", "veggies-v-opencode"]


def test_ui_dir_segment_is_base64url_no_padding():
    # the web UI's dir route param (1.18.27 bundle: btoa url-safe stripped)
    assert veggies.ui_dir_segment() == "L3dvcmtzcGFjZQ"
    assert veggies.ui_dir_segment("/x") == "L3g"


def test_pick_ui_port_default_and_fallback():
    assert veggies.pick_ui_port(4098, is_free=lambda p: True) == 5098
    # busy default + busy scan head -> next free in the scan range
    taken = {5098, 5200, 5201}
    assert veggies.pick_ui_port(4098, is_free=lambda p: p not in taken) == 5202
    with pytest.raises(ValueError):
        veggies.pick_ui_port(4098, is_free=lambda p: False)


def test_format_sessions_table_and_issue_filter():
    sessions = [
        {"id": "ses_a", "title": "#15: fix docs",
         "time": {"updated": 1789080967955}},
        {"id": "ses_b", "title": "New session", "time": {}},
        {"id": "ses_c"},
    ]
    status = {"ses_a": {"type": "busy"}}
    out = veggies.format_sessions(sessions, status)
    lines = out.splitlines()
    assert "ses_a" in lines[1] and "busy" in lines[1] and "#15" in lines[1]
    assert "09-10" in lines[1]  # epoch ms rendered as a date (UTC)
    assert lines[2].split()[1] == "idle"  # ses_b not busy
    # --issue filters on the '#N:' title prefix
    filtered = veggies.format_sessions(sessions, status, issue=15)
    assert "ses_a" in filtered and "ses_b" not in filtered
    assert veggies.format_sessions([], {}, issue=4) == "no sessions for issue #4"


def test_live_first_orders_live_before_idle_newest_first():
    sessions = [
        {"id": "ses_idle_new", "time": {"updated": 3000}},
        {"id": "ses_live_old", "time": {"updated": 1000}},
        {"id": "ses_idle_old", "time": {"updated": 2000}},
        {"id": "ses_retry"},
    ]
    status = {"ses_live_old": {"type": "busy"},
              "ses_retry": {"type": "retry"}}
    ids = [s["id"] for s in veggies.live_first(sessions, status)]
    # non-idle of any type is live; live group first, each newest-first
    assert ids == ["ses_live_old", "ses_retry", "ses_idle_new", "ses_idle_old"]
    # garbage/absent status = cannot tell -> plain newest-first, nothing lost
    degraded = [s["id"] for s in veggies.live_first(sessions, None)]
    assert degraded == ["ses_idle_new", "ses_idle_old", "ses_live_old",
                        "ses_retry"]
    assert [s["id"] for s in veggies.live_first(sessions, "garbage")] == degraded


def test_format_sessions_live_first_and_idle_cap():
    sessions = [{"id": f"ses_i{n}", "title": f"idle {n}",
                 "time": {"updated": n}} for n in range(12)]
    sessions.append({"id": "ses_live", "title": "#9: grinding",
                     "time": {"updated": 1}})  # oldest, but live
    status = {"ses_live": {"type": "busy"}}
    lines = veggies.format_sessions(sessions, status).splitlines()
    assert "ses_live" in lines[1]  # live first despite being oldest
    assert len(lines) == 1 + 11 + 1  # header + 1 live + 10 idle + footer
    assert lines[-1] == "... and 2 more idle sessions (use --all)"
    full = veggies.format_sessions(sessions, status, show_all=True)
    assert "ses_i0" in full and "ses_i11" in full and "more idle" not in full
    # --issue shows every match, uncapped
    many = [{"id": f"ses_m{n}", "title": f"#7: t {n}",
             "time": {"updated": n}} for n in range(12)]
    filtered = veggies.format_sessions(many, {}, issue=7)
    assert filtered.count("ses_m") == 12 and "more idle" not in filtered


def test_format_sessions_live_beyond_cap_still_shown():
    # the invariant runbook's stale-worktree ownership check stands on:
    # live rows are never hidden, no matter how many idle rows are newer
    sessions = [{"id": f"ses_i{n}", "time": {"updated": n}}
                for n in range(30)]
    sessions.append({"id": "ses_live", "time": {"updated": 1}})
    out = veggies.format_sessions(sessions, {"ses_live": {"type": "busy"}})
    assert "ses_live" in out


def test_format_sessions_status_outage_hides_nothing():
    # /session/status unreachable (api_call -> None): cannot tell live
    # from idle, so the cap must not engage - degrade to the full
    # newest-first table (ADR 0044).
    sessions = [{"id": f"ses_i{n}", "time": {"updated": n}}
                for n in range(30)]
    sessions.append({"id": "ses_old", "time": {"updated": 1}})
    out = veggies.format_sessions(sessions, None)
    assert "ses_old" in out and "more idle" not in out
    assert len(out.splitlines()) == 1 + 31


def test_format_sessions_footer_plural():
    eleven = [{"id": f"ses_{n}", "time": {"updated": n}} for n in range(11)]
    out = veggies.format_sessions(eleven, {})
    assert out.splitlines()[-1] == "... and 1 more idle session (use --all)"


def test_live_first_tolerates_garbage_entries_and_timestamps():
    sessions = [{"id": "ses_x", "time": {"updated": "soon"}},
                {"id": "ses_y", "time": {"updated": 5}}]
    out = veggies.live_first(sessions, {"ses_x": "busy"})
    assert [s["id"] for s in out] == ["ses_y", "ses_x"]  # no raise


def test_cmd_ui_local_prints_directly(monkeypatch, capsys):
    monkeypatch.setattr(veggies.State, "get", lambda self, n: {
        "repo": "/r", "mode": "mount", "port": 4098, "host": None,
        "password": "p"})
    monkeypatch.setattr(veggies, "api_call", lambda *a, **k: [])
    args = argparse.Namespace(name="v", port=None, stop=False,
                              open_browser=False)
    assert veggies.cmd_ui(args) == 0
    out = capsys.readouterr().out
    assert "http://127.0.0.1:4098/L3dvcmtzcGFjZQ" in out and "password: p" in out
    assert "home:   http://127.0.0.1:4098/" in out  # the all-sessions view
    assert "tunnel" not in out


def test_cmd_ui_prints_live_first_header_and_overflow(monkeypatch, capsys):
    monkeypatch.setattr(veggies.State, "get", lambda self, n: {
        "repo": "/r", "mode": "mount", "port": 4098, "host": None,
        "password": "p"})
    seven = [{"id": f"ses_{n}", "time": {"updated": n}} for n in range(7)]
    monkeypatch.setattr(
        veggies, "api_call",
        lambda *a, **k: seven if "/session?" in a[4] else {})
    args = argparse.Namespace(name="v", port=None, stop=False,
                              open_browser=False)
    assert veggies.cmd_ui(args) == 0
    out = capsys.readouterr().out
    assert "sessions (live first):" in out
    assert "... and 2 more - `veggies sessions v` lists them, live first" in out


def test_cmd_ui_remote_spawns_background_tunnel(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("VEGGIES_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(veggies.State, "get", lambda self, n: {
        "repo": "/r", "mode": "clone", "port": 4098, "host": "veggies",
        "password": "p"})
    monkeypatch.setattr(veggies, "port_free", lambda p: True)
    monkeypatch.setattr(veggies, "probe_api", lambda *a, **k: {"ok": 1})
    monkeypatch.setattr(veggies, "api_call", lambda *a, **k: [])

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

    calls = []
    monkeypatch.setattr(veggies.subprocess, "Popen",
                        lambda argv, **kw: calls.append(argv) or FakeProc())
    args = argparse.Namespace(name="v", port=None, stop=False,
                              open_browser=False)
    assert veggies.cmd_ui(args) == 0
    argv = calls[0]
    assert argv[0] == "ssh" and "-N" in argv and "veggies" in argv
    assert "5098:127.0.0.1:4098" in argv
    out = capsys.readouterr().out
    assert "http://127.0.0.1:5098/L3dvcmtzcGFjZQ" in out and "--stop" in out
    assert "home:   http://127.0.0.1:5098/" in out
    # pidfile lets a second run reuse the live tunnel
    info = json.loads((tmp_path / "tunnels" / "v.json").read_text())
    assert info == {"pid": 4242, "port": 5098}


def test_cmd_supervise_stamps_judge_call_metadata(monkeypatch):
    """ADR 0052: the operator-driven judge call's exec payload carries the
    session's title/id so the cost log attributes judge spend."""
    monkeypatch.setattr(veggies.State, "get", lambda self, n: {
        "repo": "/r", "mode": "clone", "port": 4098, "host": None,
        "password": "p"})
    msgs = [
        {"info": {"role": "user", "id": "u1"},
         "parts": [{"type": "text", "text": "work the issue"}]},
        {"info": {"role": "assistant", "id": "a1"},
         "parts": [{"type": "text", "text": "done"}]},
    ]

    def fake_api(host, port, password, method, path, *a, **k):
        if path.startswith("/session/status"):
            return {"ses_1": {"type": "idle"}}
        if "/message" in path:
            return msgs
        return {"id": "ses_1", "title": "#46: meter the spend"}

    monkeypatch.setattr(veggies, "api_call", fake_api)
    monkeypatch.setattr(veggies.time, "sleep", lambda s: None)

    class R:
        returncode = 0
        stdout = '{"score": 0.9, "issues": []}'
        stderr = ""

    execs = []
    monkeypatch.setattr(veggies, "host_podman",
                        lambda *a, **kw: (execs.append(kw), R())[1])
    args = argparse.Namespace(name="v", session="ses_1",
                              judge_model="deepseek-v4", threshold=0.6,
                              max_iters=2, timeout=60, interval=1)
    assert veggies.cmd_supervise(args) == 0  # PASS on the first judgment
    script = execs[0]["input_text"]
    b64 = [l for l in script.splitlines() if "b64decode" in l][0]
    payload = json.loads(base64.b64decode(b64.split('"')[1]))
    assert payload["metadata"] == {"caller": "veggies-supervise",
                                   "session_title": "#46: meter the spend",
                                   "session_id": "ses_1"}


def test_session_links_live_first_with_urls():
    sessions = [
        {"id": "ses_idle_new", "title": "just finished",
         "time": {"updated": 3000}},
        {"id": "ses_live_old", "title": "#15: the fix",
         "time": {"updated": 2000}},
        {"id": "ses_nokeys"},
    ]
    lines = veggies.session_links(
        sessions, {"ses_live_old": {"type": "busy"}}, "http://127.0.0.1:5098")
    # live first even though an idle session is newer
    assert lines[0].startswith("  busy") and "ses_live_old" in lines[0]
    assert "http://127.0.0.1:5098/L3dvcmtzcGFjZQ/session/ses_live_old" \
        in lines[0]
    assert "(untitled)" in lines[-1]
    assert len(veggies.session_links(sessions * 3, {}, "u", limit=5)) == 5


def test_cmd_ui_reuses_live_tunnel_and_stop_kills(monkeypatch, tmp_path):
    monkeypatch.setenv("VEGGIES_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(veggies.State, "get", lambda self, n: {
        "repo": "/r", "mode": "clone", "port": 4098, "host": "veggies",
        "password": "p"})
    tdir = tmp_path / "tunnels"
    tdir.mkdir()
    (tdir / "v.json").write_text(json.dumps({"pid": 4242, "port": 5098}))
    killed = []
    monkeypatch.setattr(veggies.os, "kill",
                        lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(veggies.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not respawn")))
    args = argparse.Namespace(name="v", port=None, stop=False,
                              open_browser=False)
    assert veggies.cmd_ui(args) == 0  # reused: os.kill(pid, 0) liveness check
    assert killed == [(4242, 0)]
    stop = argparse.Namespace(name="v", port=None, stop=True,
                              open_browser=False)
    assert veggies.cmd_ui(stop) == 0
    assert killed[-1] == (4242, 15)
    assert not (tdir / "v.json").exists()


def test_prepare_uses_local_repo_config_and_streams(monkeypatch, tmp_path,
                                                    capsys):
    (tmp_path / "veggies.yml").write_text("mcps: [toolbox]\n")
    calls = []

    def fake_ensure(host, repo, spec, verbose=False):
        calls.append((host, verbose,
                          [c.name for c in veggies.stack_components(spec)]))

    monkeypatch.setattr(veggies, "ensure_images", fake_ensure)
    args = argparse.Namespace(repo=str(tmp_path), host="veggies", name=None)
    assert veggies.cmd_prepare(args) == 0
    host, verbose, comps = calls[0]
    assert host == "veggies" and verbose is True
    assert "toolbox" in comps  # mcps from the local veggies.yml honored
    assert comps[:3] == ["opencode", "litellm", "squid"]
    out = capsys.readouterr().out
    assert "veggies up" in out and "--host veggies" in out and "--clone" in out


def test_prepare_nonlocal_repo_warns_and_defaults(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(veggies, "ensure_images",
                        lambda *a, **k: calls.append((a, k)))
    args = argparse.Namespace(repo="git@github.com:o/r.git", host=None,
                              name=None)
    assert veggies.cmd_prepare(args) == 0
    res = capsys.readouterr()
    assert "not a local checkout" in res.err
    # local target: the next-step hint stays local (no --host/--clone)
    assert "next: veggies up --repo" in res.out and "--host" not in res.out
    assert calls[0][0][0] is None  # host=None


def test_ensure_images_verbose_streams_and_quiet_default(monkeypatch, spec):
    calls = []

    class R:
        returncode = 1  # "image exists" says no -> pull paths exercised too
        stdout = ""

    monkeypatch.setattr(veggies, "run",
                        lambda cmd, **kw: (calls.append(cmd), R())[1])
    monkeypatch.setattr(veggies, "host_write", lambda *a, **k: None)
    veggies.ensure_images(None, INFRA_REPO, spec, verbose=True)
    builds = [c for c in calls if c[:2] == ["podman", "build"]]
    pulls = [c for c in calls if c[:2] == ["podman", "pull"]]
    assert builds and pulls, "default spec builds base+opencode+squid, pulls litellm"
    assert all("-q" not in b for b in builds + pulls)
    calls.clear()
    veggies.ensure_images(None, INFRA_REPO, spec)
    builds = [c for c in calls if c[:2] == ["podman", "build"]]
    assert builds and all("-q" in b for b in builds)


def test_ensure_images_builds_base_before_overlay(monkeypatch, spec):
    # ADR 0053: the overlay is FROM the locally-built base - the base must
    # build first or the overlay's FROM has nothing to resolve to.
    calls = []

    class R:
        returncode = 1  # "image exists" says no -> pull paths exercised too
        stdout = ""

    monkeypatch.setattr(veggies, "run",
                        lambda cmd, **kw: (calls.append(cmd), R())[1])
    monkeypatch.setattr(veggies, "host_write", lambda *a, **k: None)
    veggies.ensure_images(None, INFRA_REPO, spec)
    builds = [c for c in calls if c[:2] == ["podman", "build"]]
    tags = [b[b.index("-t") + 1] for b in builds]
    assert tags[:2] == [veggies_stack.IMAGE_OPENCODE_BASE,
                        veggies_stack.IMAGE_OPENCODE]


def test_up_refuses_same_name_on_other_host(monkeypatch, tmp_path):
    # The state primary key is the name: without this guard, re-upping a
    # name on a different host reuses its port and overwrites its record,
    # orphaning the original stack (hit live 2026-09-10).
    monkeypatch.setenv("VEGGIES_STATE_DIR", str(tmp_path))
    veggies.State().add(
        veggies.StackSpec(name="v", repo=str(tmp_path), port=4096,
                          host="veggies"), password="p")
    args = argparse.Namespace(repo=".", name="v", host=None, clone=False,
                              model=None, github=False, yes=True,
                              no_attach=True, no_install=True)
    with pytest.raises(ValueError, match="already exists on host"):
        veggies.cmd_up(args)


def test_up_creates_stack_dir_before_stack_config(monkeypatch, tmp_path):
    # ADR 0051/0052: hostPath type: Directory fails kube play on a missing
    # source, so cmd_up mkdirs the stack dir (the spend log's hostPath)
    # right before write_stack_config (whose chcon -R then labels it).
    monkeypatch.setenv("VEGGIES_STATE_DIR", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    events = []

    def fake_host_run(host, args, **kw):
        events.append(("run", list(args)))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(veggies, "host_run", fake_host_run)
    monkeypatch.setattr(veggies, "host_podman",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0))
    monkeypatch.setattr(veggies, "host_write",
                        lambda host, path, content, mode=0o600:
                        events.append(("write", path)))
    monkeypatch.setattr(veggies, "host_systemctl",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0))
    monkeypatch.setattr(veggies, "ensure_images", lambda *a, **k: None)
    monkeypatch.setattr(veggies, "resolve_secret_values",
                        lambda spec: {"password": "p"})
    monkeypatch.setattr(veggies, "render_secret_docs", lambda *a, **k: [])
    monkeypatch.setattr(veggies, "label_for_containers", lambda *a, **k: None)
    monkeypatch.setattr(veggies, "ensure_worktree_exclude", lambda *a, **k: None)
    monkeypatch.setattr(veggies, "wait_healthy", lambda *a, **k: None)
    monkeypatch.setattr(veggies, "linger_enabled", lambda: True)
    monkeypatch.setattr(veggies, "warm_api", lambda *a, **k: True)
    args = argparse.Namespace(repo=str(repo), name="u", host=None, clone=False,
                              model=None, github=False, yes=True,
                              no_attach=True, no_install=True)
    veggies.cmd_up(args)
    mkdir = ("run", ["mkdir", "-p", f"{tmp_path}/u"])
    assert mkdir in events
    cfg_write = next(i for i, e in enumerate(events)
                     if e[0] == "write" and "/u/config/" in e[1])
    assert events.index(mkdir) < cfg_write


def test_down_purge_removes_github_secret_via_declared_names(monkeypatch, tmp_path):
    # cmd_down --purge must remove EVERY declared secret, github's included.
    # No down/purge test precedent exists, so drive the real cmd_down with
    # the host_* seams monkeypatched (like the probe/clone tests above).
    monkeypatch.setenv("VEGGIES_STATE_DIR", str(tmp_path))
    veggies.State().add(
        veggies.StackSpec(name="g", repo="/tmp/g", port=4096, github=True),
        password="p")
    calls = []
    monkeypatch.setattr(veggies, "host_run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0))
    # host_podman's first positional is the host; drop it so captures are
    # pure podman argv and compare against the declared name list directly.
    monkeypatch.setattr(veggies, "host_podman",
                        lambda host, *a, **k: calls.append(a) or
                        subprocess.CompletedProcess(a, 0))
    monkeypatch.setattr(veggies, "host_systemctl",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0))
    monkeypatch.setattr(veggies, "host_exists", lambda *a, **k: False)
    monkeypatch.setattr(veggies, "safe_rmtree", lambda *a, **k: None)
    veggies.cmd_down(argparse.Namespace(name="g", purge=True))
    rm = next(c for c in calls if "secret" in c)
    assert "veggies-g-github" in rm
    assert rm == ("secret", "rm") + tuple(veggies.secret_names(
        veggies.spec_from_record("g", {"repo": "/tmp/g", "mode": "mount",
                                       "port": 4096, "host": None,
                                       "github": True})))


def test_down_purge_warns_before_deleting_spend_history(monkeypatch, tmp_path, capsys):
    # ADR 0051: spend.jsonl is durable history - warn once before purge
    # deletes it, but never block.
    monkeypatch.setenv("VEGGIES_STATE_DIR", str(tmp_path))
    veggies.State().add(
        veggies.StackSpec(name="c", repo="/tmp/c", port=4096), password="p")
    monkeypatch.setattr(veggies, "host_run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0))
    monkeypatch.setattr(veggies, "host_podman",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0))
    monkeypatch.setattr(veggies, "host_systemctl",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0))
    rmtree = []
    monkeypatch.setattr(veggies, "safe_rmtree",
                        lambda *a, **k: rmtree.append(a))
    spend = f"{tmp_path}/c/spend.jsonl"
    monkeypatch.setattr(veggies, "host_exists",
                        lambda host, path, kind="f": path == spend)
    veggies.cmd_down(argparse.Namespace(name="c", purge=True))
    out = capsys.readouterr().out
    assert (f"!! purge deletes {spend}* (spend history, ADR 0051) - "
            "export first if it matters") in out
    assert rmtree  # the warning never blocks the purge


def test_down_purge_no_spend_log_no_warning(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("VEGGIES_STATE_DIR", str(tmp_path))
    veggies.State().add(
        veggies.StackSpec(name="c", repo="/tmp/c", port=4096), password="p")
    monkeypatch.setattr(veggies, "host_run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0))
    monkeypatch.setattr(veggies, "host_podman",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0))
    monkeypatch.setattr(veggies, "host_systemctl",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0))
    monkeypatch.setattr(veggies, "safe_rmtree", lambda *a, **k: None)
    monkeypatch.setattr(veggies, "host_exists", lambda *a, **k: False)
    veggies.cmd_down(argparse.Namespace(name="c", purge=True))
    assert "purge deletes" not in capsys.readouterr().out


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


# --- remote mode ------------------------------------------------------


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
    # Prefix form: the harness's in-pod workspace constant is the literal
    # "/workspace", which IS the infra checkout path in the stack's own
    # clone - a bare `str(INFRA_REPO) not in text` false-positives there
    # (verified 2026-09-11 in the issue-26 session).
    assert str(INFRA_REPO) + "/" not in text
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
    veggies.host_run("veggies", ["sh", "-c", "a && b > '/p q'"])
    assert calls[0] == ["true"]
    assert calls[1] == ["ssh", "veggies", "sudo", "-n", "-u", "stacks", "id", "-u"]
    # argv becomes ONE shlex-quoted string (ssh re-joins for the remote shell)
    assert calls[2][0:2] == ["ssh", "veggies"]
    payload = calls[2][2]
    assert payload.startswith("cd / && sudo -n -u stacks env HOME=/home/stacks "
                              "XDG_RUNTIME_DIR=/run/user/ ")
    assert shlex.split(payload)[-3:] == ["sh", "-c", "a && b > '/p q'"]  # round-trips


def test_remote_clone_public_repo_gets_no_token(monkeypatch):
    calls = []

    def fake_host_run(host, args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(veggies, "host_run", fake_host_run)
    monkeypatch.setattr(veggies, "vault_key",
                        lambda *a, **k: pytest.fail("token read for a public repo"))
    cmd = veggies.remote_clone_cmd("veggies", "https://github.com/Innoptech/veggies.git", "/c/x")
    assert cmd == ["git", "-c", f"http.proxy={veggies_stack.REMOTE_PROXY}",
                   "clone", "https://github.com/Innoptech/veggies.git", "/c/x"]
    assert calls == [["git", "-c", f"http.proxy={veggies_stack.REMOTE_PROXY}",
                      "ls-remote", "https://github.com/Innoptech/veggies.git", "HEAD"]]


def test_remote_clone_normalizes_github_ssh(monkeypatch):
    calls = []

    def fake_host_run(host, args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(veggies, "host_run", fake_host_run)
    monkeypatch.setattr(veggies, "vault_key",
                        lambda *a, **k: pytest.fail("token read for a public repo"))
    cmd = veggies.remote_clone_cmd("h", "git@github.com:Innoptech/veggies.git", "/x")
    assert cmd[-2:] == ["https://github.com/Innoptech/veggies.git", "/x"]
    # normalization happens before the anonymous ls-remote probe
    assert calls == [["git", "-c", f"http.proxy={veggies_stack.REMOTE_PROXY}",
                      "ls-remote", "https://github.com/Innoptech/veggies.git", "HEAD"]]


def test_remote_clone_private_repo_gets_token(monkeypatch):
    def fake_host_run(host, args, **kwargs):
        return subprocess.CompletedProcess(args, 1)  # anonymous probe fails

    monkeypatch.setattr(veggies, "host_run", fake_host_run)
    monkeypatch.setattr(veggies, "vault_key", lambda *a, **k: "tok123")
    cmd = veggies.remote_clone_cmd("veggies", "https://github.com/Innoptech/private.git", "/c/x")
    header = "http.extraHeader=Authorization: Bearer tok123"
    # the extraHeader -c pair must come BEFORE "clone": a trailing -c is
    # git-clone's own --config and persists into the new repo's .git/config
    # (verified 2026-09-10, git 2.54); command-scoped -c never lands on disk
    assert cmd[3:5] == ["-c", header]
    assert cmd.index(header) < cmd.index("clone")
    assert cmd[-2:] == ["https://github.com/Innoptech/private.git", "/c/x"]


def test_remote_clone_non_github_never_probes(monkeypatch):
    monkeypatch.setattr(veggies, "host_run",
                        lambda *a, **k: pytest.fail("no probe for non-github URLs"))
    cmd = veggies.remote_clone_cmd("veggies", "https://gitlab.com/x/y.git", "/c/x")
    assert cmd == ["git", "-c", f"http.proxy={veggies_stack.REMOTE_PROXY}",
                   "clone", "https://gitlab.com/x/y.git", "/c/x"]


def test_warn_if_root(monkeypatch, capsys):
    monkeypatch.setattr(veggies.os, "geteuid", lambda: 0)
    veggies.warn_if_root(None)
    assert "root" in capsys.readouterr().err
    veggies.warn_if_root("veggies")  # remote stacks don't run local podman
    assert capsys.readouterr().err == ""
    monkeypatch.setattr(veggies.os, "geteuid", lambda: 1000)
    veggies.warn_if_root(None)
    assert capsys.readouterr().err == ""


def test_vault_key_surfaces_stderr(monkeypatch):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(
            1, ["vault_get.py"], stderr="some noise\n"
            "vault password file missing or empty: /nope - create it")
    monkeypatch.setattr(veggies.subprocess, "run", boom)
    with pytest.raises(ValueError, match="password file missing or empty"):
        veggies.vault_key("fireworks_api_key")


def test_vault_key_redacts_secrets_in_stderr(monkeypatch):
    veggies._SECRET_STRINGS.add("s3cret-value")
    def boom(*a, **k):
        raise subprocess.CalledProcessError(
            1, ["vault_get.py"], stderr="decryption failed for s3cret-value")
    monkeypatch.setattr(veggies.subprocess, "run", boom)
    with pytest.raises(ValueError) as err:
        veggies.vault_key("fireworks_api_key")
    assert "s3cret-value" not in str(err.value)
    assert "***" in str(err.value)
    veggies._SECRET_STRINGS.discard("s3cret-value")


def test_error_redaction_scrubs_vault_values(monkeypatch):
    monkeypatch.setattr(veggies, "host_run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1))
    monkeypatch.setattr(veggies.subprocess, "run", lambda *a, **k: type(
        "R", (), {"stdout": "tok123\n"})())
    veggies.vault_key("github_token", "secrets/github.yml")
    err = 'Command [\'git\', \'-c\', \'http.extraHeader=Authorization: Bearer tok123\'] failed'
    assert veggies.redact(err) == err.replace("tok123", "***")
    assert "tok123" in err  # sanity: the raw text did contain it


def test_stack_url_local_and_remote():
    assert veggies.stack_url({"host": None, "port": 4096}) == "http://127.0.0.1:4096"
    assert veggies.stack_url({"host": "veggies", "port": 4097}) == "http://veggies:4097"


def test_watchdog_units_are_minimal_and_scoped():
    assert 'label=app=veggies' in veggies.WATCHDOG_SERVICE
    assert 'status=exited' in veggies.WATCHDOG_SERVICE
    assert "OnUnitActiveSec" in veggies.WATCHDOG_TIMER
    assert "WantedBy=timers.target" in veggies.WATCHDOG_TIMER


# --- veggies sync ---------------------------------------------------------------


def test_github_https_url_normalizes_ssh_form():
    assert veggies.github_https_url("git@github.com:Org/r.git") == \
        "https://github.com/Org/r.git"
    assert veggies.github_https_url("https://github.com/Org/r.git") == \
        "https://github.com/Org/r.git"
    assert veggies.github_https_url("https://example.com/r.git") == \
        "https://example.com/r.git"


def test_clone_pull_argv_local_is_plain_ff_only():
    argv = veggies.clone_pull_argv("/state/clones/demo")
    assert argv == ["git", "-C", "/state/clones/demo", "pull", "--ff-only"]


def test_clone_pull_argv_remote_rides_the_proxy():
    argv = veggies.clone_pull_argv("/home/stacks/.local/state/veggies/clones/d",
                                   proxy="http://127.0.0.1:3128")
    assert "-c" in argv and f"http.proxy=http://127.0.0.1:3128" in argv
    assert argv[-2:] == ["pull", "--ff-only"]
    # -C and -c are both leading options: the subcommand stays last, and no
    # config is ever persisted into the clone's .git/config.
    assert argv.index("-C") < argv.index("pull")


def test_clone_pull_argv_token_is_command_scoped():
    argv = veggies.clone_pull_argv("/c", proxy="http://p", token="tok123")
    assert "http.extraHeader=Authorization: Bearer tok123" in argv
    assert argv[-2:] == ["pull", "--ff-only"]


def test_busy_titles_filters_by_status():
    sessions = [{"id": "s1", "title": "#1: a"}, {"id": "s2", "title": "#2: b"}]
    status = {"s1": {"type": "busy"}, "s2": {"type": "idle"}}
    assert veggies.busy_titles(sessions, status) == ["#1: a"]
    assert veggies.busy_titles(sessions, {}) == []
    # off-shape payloads degrade to "cannot tell" = proceed
    assert veggies.busy_titles(None, status) == []
    assert veggies.busy_titles(sessions, "garbage") == []
    # a busy session without a title still identifies itself
    assert veggies.busy_titles([{"id": "s1"}], status) == ["s1"]


def test_sync_rejects_unknown_stack(tmp_path, monkeypatch):
    state_cls = veggies.State  # capture before patching (lambda self-reference)
    monkeypatch.setattr(veggies, "State", lambda: state_cls(root=tmp_path))
    args = argparse.Namespace(name="nope", force=False)
    with pytest.raises(ValueError, match="unknown stack"):
        veggies.cmd_sync(args)


def test_sync_refuses_mount_mode(tmp_path, monkeypatch):
    state_cls = veggies.State
    monkeypatch.setattr(veggies, "State", lambda: state_cls(root=tmp_path))
    state_cls(root=tmp_path).add(
        veggies.StackSpec(name="m", repo="/tmp/m", mode="mount", port=4096))
    args = argparse.Namespace(name="m", force=False)
    with pytest.raises(ValueError, match="mount-mode"):
        veggies.cmd_sync(args)


def test_cmd_ls_prints_every_stack_on_record(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VEGGIES_STATE_DIR", str(tmp_path))
    veggies.State().add(veggies.StackSpec(name="aaa", repo="/tmp/aaa",
                                          port=4096))
    veggies.State().add(veggies.StackSpec(name="bbb", repo="/tmp/bbb",
                                          port=4097))
    monkeypatch.setattr(veggies, "host_podman", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, stdout="",
                                                    stderr=""))
    monkeypatch.setattr(veggies, "host_exists", lambda *a, **k: False)
    assert veggies.main(["ls"]) == 0
    out = capsys.readouterr().out
    assert "aaa" in out and "bbb" in out


# --- golden file ----------------------------------------------------------------


def test_render_matches_golden(monkeypatch):
    monkeypatch.setenv("VEGGIES_STATE_DIR", str(FIXED_STATE))
    fixed = veggies.StackSpec(
        name="demo", repo="/tmp/veggies-test-state/demo-repo", mode="mount", port=4096
    )
    golden = (ROOT / "tests/golden/pod.yaml").read_text()
    # Prefix replace: a blanket replace of ROOT breaks when the checkout IS
    # "/workspace" (the stack's own clone) - it would also rewrite the
    # harness's constant in-pod `mountPath: /workspace`.
    rendered = veggies.render_yaml(fixed, INFRA_REPO).replace(str(ROOT) + "/", "@ROOT@/")
    assert rendered == golden
