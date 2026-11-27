from __future__ import annotations

from tt_auto_bag_scanner.application.import_service import suggest_column_mapping

# 标准“出库包裹报表”模板的表头（仅列名，不含数据）。
STANDARD_HEADERS = (
    "包裹号",
    "物流运单号",
    "出库单号",
    "大包号",
    "包裹状态",
    "快递公司",
    "Sort Code",
    "复核完成时间",
    "出库时间",
    "组大包完成时间",
)


def test_standard_template_maps_all_columns_exactly() -> None:
    assert suggest_column_mapping(STANDARD_HEADERS) == {
        "tracking": "物流运单号",
        "carrier": "快递公司",
        "status": "包裹状态",
        "outbound_at": "出库时间",
        "sort_code": "Sort Code",
    }


def test_generated_english_template_headers_are_mapped() -> None:
    headers = ("tracking", "carrier", "status", "outbound_at", "sort_code")

    assert suggest_column_mapping(headers) == {key: key for key in headers}


def test_headers_are_trimmed_but_not_fuzzy_matched() -> None:
    mapping = suggest_column_mapping((" 物流运单号 ", "出库时间（本地）", "快递公司名称"))

    assert mapping == {"tracking": " 物流运单号 "}


def test_duplicate_matching_headers_are_not_selected() -> None:
    mapping = suggest_column_mapping(("物流运单号", "物流运单号", "快递公司"))

    assert "tracking" not in mapping
    assert mapping["carrier"] == "快递公司"


def test_saved_selection_wins_only_when_still_present() -> None:
    mapping = suggest_column_mapping(
        STANDARD_HEADERS,
        saved={"tracking": "包裹号", "carrier": "已删除的列", "status": ""},
    )

    assert mapping["tracking"] == "包裹号"
    assert mapping["carrier"] == "快递公司"
    assert mapping["status"] == "包裹状态"
