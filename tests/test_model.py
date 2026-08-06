import pytest

from harness_eval.model import CaseError, check_drift, load_case, load_cases, sha256_file

REPO_CASES = "cases"


def write_case(tmp_path, body: str, fragment: str = "frag") -> object:
    (tmp_path / "f.md").write_text(fragment, encoding="utf-8")
    path = tmp_path / "case.toml"
    path.write_text(body, encoding="utf-8")
    return path


MINIMAL = """
schema = 1
id = "demo"
[target]
kind = "rule"
[probe]
prompt = "hi"
[arms]
fragment = "f.md"
[[checks]]
kind = "contains"
pattern = "x"
"""


def test_minimal_case_loads(tmp_path):
    case = load_case(write_case(tmp_path, MINIMAL))
    assert case.id == "demo"
    assert case.n == 6
    assert case.tools == ("",)
    assert case.models == ("opus", "sonnet")


def test_case_must_vary_something(tmp_path):
    body = MINIMAL.replace('fragment = "f.md"', "")
    with pytest.raises(CaseError, match="vary something"):
        load_case(write_case(tmp_path, body))


def test_missing_fragment_is_rejected(tmp_path):
    body = MINIMAL.replace('fragment = "f.md"', 'fragment = "gone.md"')
    with pytest.raises(CaseError, match="missing file"):
        load_case(write_case(tmp_path, body))


def test_unknown_check_kind_is_rejected(tmp_path):
    body = MINIMAL.replace('kind = "contains"', 'kind = "vibes"')
    with pytest.raises(CaseError, match="not in"):
        load_case(write_case(tmp_path, body))


def test_pattern_is_required_for_regex_checks(tmp_path):
    body = MINIMAL.replace('pattern = "x"', "")
    with pytest.raises(CaseError, match="requires 'pattern'"):
        load_case(write_case(tmp_path, body))


def test_unsupported_schema_is_rejected(tmp_path):
    with pytest.raises(CaseError, match="unsupported schema"):
        load_case(write_case(tmp_path, MINIMAL.replace("schema = 1", "schema = 99")))


def test_codex_case_requires_codex_project_isolation(tmp_path):
    body = MINIMAL.replace("schema = 1", 'schema = 1\nengine = "codex"')
    with pytest.raises(CaseError, match="codex-project"):
        load_case(write_case(tmp_path, body))


def test_skill_canary_requires_a_skill(tmp_path):
    body = MINIMAL.replace('fragment = "f.md"', 'fragment = "f.md"\nskill_canary = "X"')
    with pytest.raises(CaseError, match="requires arms.skill"):
        load_case(write_case(tmp_path, body))


def test_duplicate_ids_are_rejected(tmp_path):
    (tmp_path / "f.md").write_text("frag", encoding="utf-8")
    for name in ("a.toml", "b.toml"):
        (tmp_path / name).write_text(MINIMAL, encoding="utf-8")
    with pytest.raises(CaseError, match="duplicate case id"):
        load_cases(tmp_path)


def test_repo_cases_are_all_valid():
    # Loading must not raise for whatever corpus is present locally. The
    # personal case corpus is untracked, so only the selftest cases are
    # guaranteed to exist — asserted separately below.
    load_cases_from_repo()


def load_cases_from_repo():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    return load_cases(root / REPO_CASES, recursive=True)


def test_repo_selftest_cases_exist():
    ids = {c.id for c in load_cases_from_repo()}
    assert {"_selftest-injection", "_selftest-hook"} <= ids


def test_drift_detects_a_changed_source(tmp_path):
    src = tmp_path / "live.md"
    src.write_text("original", encoding="utf-8")
    body = MINIMAL.replace('kind = "rule"',
                           f'kind = "rule"\nsource = "{src}"\nsha256 = "{sha256_file(src)}"')
    case = load_case(write_case(tmp_path, body))
    assert check_drift([case])[0].status == "ok"

    src.write_text("edited", encoding="utf-8")
    assert check_drift([case])[0].status == "changed"

    src.unlink()
    assert check_drift([case])[0].status == "missing"


def test_excise_cuts_exact_line_ranges():
    from harness_eval.model import excise
    text = "l1\nl2\nl3\nl4\nl5\n"
    remaining, removed = excise(text, ((2, 3),))
    assert remaining == "l1\nl4\nl5\n"
    assert removed == "l2\nl3\n"


def test_excise_handles_multiple_ranges():
    from harness_eval.model import excise
    text = "".join(f"l{i}\n" for i in range(1, 11))
    remaining, removed = excise(text, ((1, 1), (5, 7)))
    assert removed == "l1\nl5\nl6\nl7\n"
    assert "l5" not in remaining and "l4" in remaining


def test_excise_rejects_out_of_range():
    from harness_eval.model import excise
    with pytest.raises(ValueError, match="exceeds document"):
        excise("one\ntwo\n", ((1, 99),))


EXCISION = """
schema = 1
id = "exc"
[target]
kind = "rule"
source = "doc.md"
sha256 = "{sha}"
excise_lines = [[2, 2]]
[probe]
prompt = "hi"
[arms]
mode = "excision"
[[checks]]
kind = "contains"
pattern = "x"
"""


def make_excision_case(tmp_path, sha=None):
    doc = tmp_path / "doc.md"
    doc.write_text("keep1\nRULE LINE\nkeep2\n", encoding="utf-8")
    real_sha = sha or sha256_file(doc)
    path = tmp_path / "case.toml"
    path.write_text(EXCISION.format(sha=real_sha), encoding="utf-8")
    return load_case(path)


def test_excision_case_loads_without_a_fragment(tmp_path):
    case = make_excision_case(tmp_path)
    assert case.mode == "excision"
    assert case.target.excise_lines == ((2, 2),)


def test_excision_requires_a_pinned_sha(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("keep1\nRULE LINE\nkeep2\n", encoding="utf-8")
    body = EXCISION.format(sha="x").replace('sha256 = "x"\n', "")
    path = tmp_path / "case.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(CaseError, match="sha256"):
        load_case(path)


def test_overlapping_excise_ranges_are_rejected(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("a\nb\nc\n", encoding="utf-8")
    body = EXCISION.format(sha=sha256_file(doc)).replace(
        "excise_lines = [[2, 2]]", "excise_lines = [[1, 2], [2, 3]]")
    path = tmp_path / "case.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(CaseError, match="overlap"):
        load_case(path)
