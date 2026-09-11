"""Tests for scripts/adr_index.py - the generated docs/adr/README.md
index table (issue #72, ADR 0053)."""

import pytest

from scripts import adr_index


def write_adr(adr_dir, name, status="accepted", date="2026-09-11", h1=None):
    """Write a minimal ADR; status/date=None omits that frontmatter key."""
    number = name.split("-", 1)[0]
    if h1 is None:
        h1 = f"# {number}. Decision {number}"
    lines = ["---"]
    if status is not None:
        lines.append(f"status: {status}")
    if date is not None:
        lines.append(f"date: {date}")
    lines += ["---", "", h1]
    path = adr_dir / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# collect()


def test_collect_parses_a_valid_adr(tmp_path):
    write_adr(tmp_path, "0043-interim-shared-identity-trigger.md",
              status="accepted (interim - sunsets at X)", date="2026-09-10",
              h1="# 0043. Interim: the shared identity may trigger kicks")
    (adr,) = adr_index.collect(tmp_path)
    assert adr.number == "0043"
    assert adr.filename == "0043-interim-shared-identity-trigger.md"
    assert adr.title == "Interim: the shared identity may trigger kicks"
    assert adr.status == "accepted (interim - sunsets at X)"
    assert adr.date == "2026-09-10"


def test_collect_sorts_by_number_regardless_of_glob_order(tmp_path):
    write_adr(tmp_path, "0010-crowdsec-auditd-no-fail2ban.md")
    write_adr(tmp_path, "0002-public-cloud-over-vps.md")
    numbers = [adr.number for adr in adr_index.collect(tmp_path)]
    assert numbers == ["0002", "0010"]


def test_collect_skips_readme_and_the_madr_template(tmp_path):
    (tmp_path / "README.md").write_text("# not an ADR\n", encoding="utf-8")
    (tmp_path / adr_index.TEMPLATE).write_text(
        "---\nstatus: proposed\ndate: YYYY-MM-DD\n---\n\n# NNNN. Title\n",
        encoding="utf-8")
    write_adr(tmp_path, "0001-record-architecture-decisions.md")
    adrs = adr_index.collect(tmp_path)
    assert [adr.number for adr in adrs] == ["0001"]


def test_collect_rejects_a_nonconforming_filename(tmp_path):
    (tmp_path / "notes.md").write_text(
        "---\nstatus: accepted\ndate: 2026-09-11\n---\n\n# 0001. Notes\n",
        encoding="utf-8")
    with pytest.raises(adr_index.AdrIndexError) as excinfo:
        adr_index.collect(tmp_path)
    assert ("notes.md: filename does not match 'NNNN-<slug>.md'"
            in str(excinfo.value))


def test_collect_rejects_duplicate_numbers_naming_both_files(tmp_path):
    write_adr(tmp_path, "0043-first.md")
    write_adr(tmp_path, "0043-second.md")
    with pytest.raises(adr_index.AdrIndexError) as excinfo:
        adr_index.collect(tmp_path)
    assert ("0043-second.md: duplicate ADR number 0043 "
            "(also 0043-first.md)") in str(excinfo.value)


@pytest.mark.parametrize("content", [
    "status: accepted\ndate: 2026-09-11\n\n# 0001. No fences at all\n",
    "---\nstatus: accepted\ndate: 2026-09-11\n\n# 0001. Never closed\n",
])
def test_collect_rejects_missing_frontmatter_fences(tmp_path, content):
    (tmp_path / "0001-a.md").write_text(content, encoding="utf-8")
    with pytest.raises(adr_index.AdrIndexError) as excinfo:
        adr_index.collect(tmp_path)
    assert "0001-a.md: missing frontmatter fences" in str(excinfo.value)


def test_collect_rejects_a_missing_status_key(tmp_path):
    write_adr(tmp_path, "0001-a.md", status=None)
    with pytest.raises(adr_index.AdrIndexError) as excinfo:
        adr_index.collect(tmp_path)
    assert "0001-a.md: missing 'status:' in frontmatter" in str(excinfo.value)


def test_collect_rejects_a_missing_date_key(tmp_path):
    write_adr(tmp_path, "0001-a.md", date=None)
    with pytest.raises(adr_index.AdrIndexError) as excinfo:
        adr_index.collect(tmp_path)
    assert "0001-a.md: missing 'date:' in frontmatter" in str(excinfo.value)


def test_collect_rejects_a_non_iso_date(tmp_path):
    write_adr(tmp_path, "0001-a.md", date="2026-13-99")
    with pytest.raises(adr_index.AdrIndexError) as excinfo:
        adr_index.collect(tmp_path)
    assert "invalid date '2026-13-99'" in str(excinfo.value)


def test_collect_rejects_an_unknown_status(tmp_path):
    write_adr(tmp_path, "0001-a.md", status="draft")
    with pytest.raises(adr_index.AdrIndexError) as excinfo:
        adr_index.collect(tmp_path)
    assert str(excinfo.value) == (
        "0001-a.md: unknown status 'draft' (proposed | accepted | rejected "
        "| deprecated | superseded by ADR-NNNN; parenthetical allowed)")


@pytest.mark.parametrize("status", [
    "proposed",
    "accepted",
    "rejected",
    "deprecated",
    "accepted (amended by 0042)",
    "accepted (interim - sunsets at X)",
    "superseded by ADR-0026",
    "superseded by ADR-0026 (see also 0028)",
])
def test_collect_accepts_every_documented_status(tmp_path, status):
    write_adr(tmp_path, "0001-a.md", status=status)
    (adr,) = adr_index.collect(tmp_path)
    assert adr.status == status


def test_collect_rejects_an_h1_number_mismatch(tmp_path):
    write_adr(tmp_path, "0043-x.md", h1="# 0044. X")
    with pytest.raises(adr_index.AdrIndexError) as excinfo:
        adr_index.collect(tmp_path)
    assert ("0043-x.md: H1 number 0044 does not match filename number 0043"
            in str(excinfo.value))


def test_collect_rejects_an_h1_without_the_number_prefix(tmp_path):
    write_adr(tmp_path, "0043-x.md", h1="# A title without a number")
    with pytest.raises(adr_index.AdrIndexError) as excinfo:
        adr_index.collect(tmp_path)
    assert "first H1 lacks the 'NNNN. ' prefix" in str(excinfo.value)


def test_collect_rejects_a_file_with_no_h1(tmp_path):
    (tmp_path / "0001-a.md").write_text(
        "---\nstatus: accepted\ndate: 2026-09-11\n---\n\n## Only a section\n",
        encoding="utf-8")
    with pytest.raises(adr_index.AdrIndexError) as excinfo:
        adr_index.collect(tmp_path)
    assert "0001-a.md: no H1" in str(excinfo.value)


def test_collect_reads_only_the_first_h1(tmp_path):
    """0017 carries a second `# ` line deep in its body; first match wins."""
    (tmp_path / "0017-a.md").write_text(
        "---\nstatus: superseded by ADR-0026\ndate: 2026-09-04\n---\n\n"
        "# 0017. First title\n\nbody\n\n# 0099. Second H1 deep in it\n",
        encoding="utf-8")
    (adr,) = adr_index.collect(tmp_path)
    assert adr.title == "First title"


def test_collect_turns_a_decode_failure_into_a_collected_error(tmp_path):
    (tmp_path / "0001-a.md").write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(adr_index.AdrIndexError) as excinfo:
        adr_index.collect(tmp_path)
    assert "0001-a.md: not valid UTF-8" in str(excinfo.value)


def test_collect_reports_every_broken_file_in_one_error(tmp_path):
    (tmp_path / "0001-a.md").write_text("no fences here\n", encoding="utf-8")
    write_adr(tmp_path, "0002-b.md", status=None)
    write_adr(tmp_path, "0003-c.md")  # a valid file does not abort the run
    with pytest.raises(adr_index.AdrIndexError) as excinfo:
        adr_index.collect(tmp_path)
    message = str(excinfo.value)
    assert "0001-a.md: missing frontmatter fences" in message
    assert "0002-b.md: missing 'status:' in frontmatter" in message


# render_table()


def test_render_table_is_byte_exact():
    adrs = [
        adr_index.ADR(number="0001",
                      filename="0001-record-architecture-decisions.md",
                      title="Record architecture decisions",
                      status="accepted", date="2026-09-01"),
        adr_index.ADR(number="0017",
                      filename="0017-agent-orchestrator-and-workflows.md",
                      title="Agent orchestrator and adaptive pipelines",
                      status="superseded by ADR-0026", date="2026-09-04"),
    ]
    assert adr_index.render_table(adrs) == (
        "| ADR | Title | Status |\n"
        "|-----|-------|--------|\n"
        "| [0001](0001-record-architecture-decisions.md) | Record "
        "architecture decisions | accepted |\n"
        "| [0017](0017-agent-orchestrator-and-workflows.md) | Agent "
        "orchestrator and adaptive pipelines | superseded by ADR-0026 |\n")


def test_render_table_with_no_adrs_renders_the_header_only():
    assert adr_index.render_table([]) == (
        "| ADR | Title | Status |\n"
        "|-----|-------|--------|\n")


# extract_region()


def test_extract_region_round_trips_a_rendered_table():
    table = adr_index.render_table([
        adr_index.ADR(number="0001", filename="0001-a.md",
                      title="Decision 0001", status="accepted",
                      date="2026-09-11"),
    ])
    text = f"head\n{adr_index.START}\n{table}{adr_index.END}\ntail\n"
    assert adr_index.extract_region(text) == table


@pytest.mark.parametrize("text", [
    "no markers at all\n",
    f"{adr_index.START}\nstart only, no end\n",
    f"end only, no start\n{adr_index.END}\n",
])
def test_extract_region_requires_both_markers(text):
    with pytest.raises(adr_index.AdrIndexError) as excinfo:
        adr_index.extract_region(text)
    assert str(excinfo.value) == (
        "docs/adr/README.md: missing adr-index markers")


def test_extract_region_rejects_end_before_start():
    text = f"{adr_index.END}\n{adr_index.START}\n"
    with pytest.raises(adr_index.AdrIndexError) as excinfo:
        adr_index.extract_region(text)
    assert str(excinfo.value) == (
        "docs/adr/README.md: adr-index:end before adr-index:start")


# rewrite_readme()


def test_rewrite_readme_replaces_only_the_region_bytes(tmp_path):
    readme = tmp_path / "README.md"
    prefix = f"# ADRs\n\nprose\n{adr_index.START}\n"
    suffix = f"{adr_index.END}\n\nkeeping this tail\n"
    readme.write_text(prefix + "stale table\n" + suffix, encoding="utf-8")
    table = adr_index.render_table([
        adr_index.ADR(number="0001", filename="0001-a.md",
                      title="Decision 0001", status="accepted",
                      date="2026-09-11"),
    ])
    assert adr_index.rewrite_readme(readme, table) is True
    rewritten = readme.read_text(encoding="utf-8")
    assert rewritten == prefix + table + suffix
    # an in-sync second call is a no-op: False, and the file is not written
    before = readme.stat().st_mtime_ns
    assert adr_index.rewrite_readme(readme, table) is False
    assert readme.read_text(encoding="utf-8") == rewritten
    assert readme.stat().st_mtime_ns == before


# main()


@pytest.fixture()
def adr_repo(tmp_path, monkeypatch):
    """Point adr_index at a synthetic ADR dir + README under tmp_path."""
    adr_dir = tmp_path / "adr"
    adr_dir.mkdir()
    readme = tmp_path / "README.md"
    monkeypatch.setattr(adr_index, "ADR_DIR", adr_dir)
    monkeypatch.setattr(adr_index, "README", readme)
    return adr_dir, readme


def test_main_returns_0_when_the_index_is_in_sync(adr_repo, capsys):
    adr_dir, readme = adr_repo
    write_adr(adr_dir, "0001-a.md")
    table = adr_index.render_table(adr_index.collect(adr_dir))
    readme.write_text(f"head\n{adr_index.START}\n{table}{adr_index.END}\n",
                      encoding="utf-8")
    assert adr_index.main() == 0
    assert capsys.readouterr().err == ""


def test_main_returns_1_and_rewrites_the_readme_on_drift(adr_repo, capsys):
    adr_dir, readme = adr_repo
    write_adr(adr_dir, "0001-a.md")
    readme.write_text(f"head\n{adr_index.START}\nstale\n{adr_index.END}\n",
                      encoding="utf-8")
    assert adr_index.main() == 1
    assert ("docs/adr/README.md: index regenerated - stage the change"
            in capsys.readouterr().err)
    synced = adr_index.render_table(adr_index.collect(adr_dir))
    assert readme.read_text(encoding="utf-8") == (
        f"head\n{adr_index.START}\n{synced}{adr_index.END}\n")
    assert adr_index.main() == 0  # converged: a second run is clean


def test_main_returns_2_on_validation_errors(adr_repo, capsys):
    adr_dir, readme = adr_repo
    (adr_dir / "0001-a.md").write_text("garbage\n", encoding="utf-8")
    readme.write_text(f"{adr_index.START}\n{adr_index.END}\n",
                      encoding="utf-8")
    assert adr_index.main() == 2
    assert "0001-a.md: missing frontmatter fences" in capsys.readouterr().err
    assert readme.read_text(encoding="utf-8") == (
        f"{adr_index.START}\n{adr_index.END}\n")  # README untouched
