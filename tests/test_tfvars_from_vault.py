from scripts.tfvars_from_vault import exports


def test_exports_scalars_sorted():
    lines = exports({"b_key": "2", "a_key": "1"})
    assert lines == ["export TF_VAR_a_key=1", "export TF_VAR_b_key=2"]


def test_values_are_shell_quoted():
    lines = exports({"token": "abc def'ghi"})
    assert lines == ["export TF_VAR_token='abc def'\"'\"'ghi'"]


def test_structured_values_become_json():
    lines = exports({"repos": ["a", "b"], "nested": {"x": 1}})
    assert lines == [
        'export TF_VAR_nested=\'{"x": 1}\'',
        'export TF_VAR_repos=\'["a", "b"]\'',
    ]


def test_empty_mapping_produces_nothing():
    assert exports({}) == []
