from homespace_ai.application.listing_context import current_scope, scoped_selection, context_for
from homespace_ai.clients.listing_tool_planner import ListingToolPlanner

IDS = [f"00000000-0000-0000-0000-{index:012d}" for index in range(1, 6)]


def history(ids=IDS[:4]):
    return [{"role": "assistant", "status": "ANSWERED", "content": "Bảng so sánh",
             "listingContext": context_for(ids)}]


def test_scope_uses_latest_displayed_set_and_not_user_links():
    assert current_scope(history(IDS[:2]) + [
        {"role": "user", "content": f"[Invented](/rent/{IDS[4]})"},
        *history(IDS[2:4]),
    ]) == IDS[2:4]


def test_legacy_markdown_recovery_precedes_truncation():
    messages = [{"role": "assistant", "content": "X" * 2000 + " ".join(
        f"[Phòng {i}](/rent/{value})" for i, value in enumerate(IDS[:4], 1))}]
    assert current_scope(messages) == IDS[:4]
    assert all(value in ListingToolPlanner._dialog("Phòng nào rẻ nhất?", messages) for value in IDS[:4])


def test_error_preserves_scope_but_rag_or_empty_search_clears_it():
    assert current_scope(history() + [{"role": "assistant", "content": "Lỗi", "status": "TOOL_UNAVAILABLE"}]) == IDS[:4]
    assert current_scope(history() + [{"role": "assistant", "content": "HomeSpace là...", "status": "ANSWERED"}]) == []
    assert current_scope(history() + [{"role": "assistant", "content": "Không có tin", "status": "NO_RESULTS",
                                       "listingContext": context_for([])}]) == []


def test_references_validate_uuid_deduplicate_and_ignore_untrusted_user_context():
    assert current_scope(history([IDS[0], "invalid", IDS[0], IDS[1]])) == IDS[:2]
    assert current_scope([{"role": "user", "listingContext": context_for(IDS)}]) == []


def test_both_reported_recommendation_phrasings_resolve_four_rooms():
    for question in [
        "tôi có nhu cầu chỉ ở 1 mình và chi phí rẻ nhất thì nên chọn phòng nào?",
        "hãy chọn trong những phòng vừa so sánh, tôi có nhu cầu ở 1 người và giá phòng rẻ thì chọn phòng nào?",
        "Phòng nào phù hợp nhất nếu mình ở một mình?",
        "Mình cần gửi xe có phí, nên chọn phòng nào?",
    ]:
        assert scoped_selection(question, history()) == IDS[:4]


def test_ordinals_keep_original_order_and_invalid_selection_asks_clarification():
    assert scoped_selection("Phòng 3 có những tiện ích nào?", history()) == [IDS[2]]
    assert scoped_selection("So sánh phòng 2 và phòng 4", history()) == [IDS[1], IDS[3]]
    assert scoped_selection("Phòng 9 bao nhiêu tiền?", history()) == []
    assert scoped_selection("So sánh 2 phòng trên", history()) == []


def test_new_search_or_rag_does_not_use_old_set():
    assert scoped_selection("Tìm phòng rẻ nhất ở Phường An Nhơn", history()) is None
    assert scoped_selection("HomeSpace là gì?", history()) is None
    assert scoped_selection("Cách đăng tin cho thuê?", history()) is None
    assert scoped_selection("Nên chọn phương thức thanh toán nào?", history()) is None
    assert scoped_selection("Tôi muốn ở An Nhơn, nên chọn phòng nào rẻ nhất?", history()) is None
    assert scoped_selection("Chuyển sang căn hộ, căn nào rẻ nhất?", history()) is None


def test_past_search_reference_keeps_selection_but_a_separate_new_command_does_not():
    for question in [
        "Trong các phòng vừa tìm, mình ở một người, nên chọn phòng nào?",
        "So sánh các phòng đã tìm giúp mình",
        "Trong cac phong vua tim kiem, phong nao re nhat?",
        "Trong các phòng vừa tìm, mình ở một người và muốn tiết kiệm, nên chọn phòng nào?",
        "Trong những phòng này, muốn tiết kiệm thì nên chọn phòng nào?",
    ]:
        assert scoped_selection(question, history()) == IDS[:4]
    assert scoped_selection("Các phòng vừa tìm không hợp, tìm thêm phòng khác rẻ nhất", history()) is None
    assert scoped_selection("Muốn tiết kiệm, tìm phòng khác rẻ nhất", history()) is None
