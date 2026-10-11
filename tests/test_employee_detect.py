"""Tests for _detect_columns — employee Excel header heuristic."""
import pytest
from employee_match import _detect_columns


class TestDetectColumns:
    def test_standard_headers(self):
        headers = ["姓名", "部门", "移动电话", "状态", "大区"]
        cols = _detect_columns(headers)
        assert cols["name"] == 0
        assert cols["dept"] == 1
        assert cols["phone"] == 2
        assert cols["status"] == 3
        assert cols["region"] == 4

    def test_alternative_name_patterns(self):
        for h in ["员工姓名", "用户名", "名字", "名称"]:
            cols = _detect_columns([h])
            assert cols["name"] == 0, f"pattern mismatch for '{h}'"

    def test_name_at_different_column(self):
        headers = ["部门", "员工姓名", "手机"]
        cols = _detect_columns(headers)
        assert cols["name"] == 1
        assert cols["dept"] == 0
        assert cols["phone"] == 2

    def test_file_name_header_not_matched_as_name(self):
        """'文件名' header must NOT be detected as the name column."""
        headers = ["文件名", "路径", "大小"]
        cols = _detect_columns(headers)
        assert cols["name"] is None
        assert cols["dept"] is None

    def test_user_related_but_not_name(self):
        """Headers containing user-related terms that aren't name patterns."""
        headers = ["用户权限", "用户ID", "账号类型"]
        cols = _detect_columns(headers)
        assert cols["name"] is None

    def test_empty_headers(self):
        cols = _detect_columns([])
        assert cols["name"] is None
        assert cols["dept"] is None
        assert cols["phone"] is None

    def test_header_with_leading_trailing_spaces(self):
        """Header matching should be substring-based, so spaces around
        the value don't affect matching as long as the pattern is inside."""
        headers = ["  姓名  ", "部门"]
        cols = _detect_columns(headers)
        # '姓名' is a substring of '  姓名  '
        assert cols["name"] == 0

    def test_phone_patterns(self):
        for h in ["移动电话", "手机", "电话", "手机号", "联系电话", "联系方式"]:
            cols = _detect_columns([h])
            assert cols["phone"] == 0, f"phone pattern mismatch for '{h}'"

    def test_dept_patterns(self):
        for h in ["部门", "所属部门", "组织", "机构", "部门名称"]:
            cols = _detect_columns([h])
            assert cols["dept"] == 0, f"dept pattern mismatch for '{h}'"

    def test_status_patterns(self):
        for h in ["禁用", "状态", "启用", "在职状态", "账号状态"]:
            cols = _detect_columns([h])
            assert cols["status"] == 0, f"status pattern mismatch for '{h}'"

    def test_region_patterns(self):
        for h in ["大区", "区域", "地区", "片区", "所属大区"]:
            cols = _detect_columns([h])
            assert cols["region"] == 0, f"region pattern mismatch for '{h}'"

    def test_partial_match_of_multi_char_pattern(self):
        """Single char from a pattern shouldn't match — only full patterns."""
        headers = ["名", "姓", "号"]
        cols = _detect_columns(headers)
        # '名' alone doesn't match any multi-char name pattern
        assert cols["name"] is None

    def test_multiple_name_patterns_picks_first(self):
        """When multiple name patterns match, first in priority list wins."""
        headers = ["员工姓名", "名称", "用户名"]
        cols = _detect_columns(headers)
        # First column matches '员工姓名' (highest priority)
        assert cols["name"] == 0

    @pytest.mark.parametrize("headers,expected_name_col", [
        (["姓名", "部门"], 0),
        (["部门", "姓名"], 1),
        (["A", "B", "员工姓名"], 2),
        (["项目名称", "真实姓名"], 1),  # '项目名称' matches '名称'
        (["无关联列"], None),
    ])
    def test_name_column_detection(self, headers, expected_name_col):
        cols = _detect_columns(headers)
        assert cols["name"] == expected_name_col
