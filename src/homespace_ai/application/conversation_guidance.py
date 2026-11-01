"""Action-aware follow-up questions shared by every chat domain; no extra model or HTTP calls."""
from homespace_ai.application.answer_context import AnswerContext, listing_focus
from homespace_ai.application.vietnamese_text import fold_text

ERRORS = {"TOOL_UNAVAILABLE", "GENERATION_UNAVAILABLE", "LOGIN_REQUIRED", "NOT_FOUND", "OUT_OF_SCOPE", "NO_EVIDENCE"}


def finish(result, question, history, domain=None):
    if result is None:
        return result
    domain = domain or ("APPOINTMENT" if result.appointment_context or "CORE_APPOINTMENT" in result.data_sources else
                        "CONTRACT" if result.contract_context or "CORE_CONTRACT" in result.data_sources else
                        "LISTING" if result.listing_context else "KNOWLEDGE")
    context = result.listing_context or {}
    scope = (context.get("listingIds") or []) if domain == "LISTING" else (
        (result.contract_context or {}).get("contractIds", []) if domain == "CONTRACT" else
        (result.appointment_context or {}).get("appointmentIds", []))
    intent = result.answer_context.get("intent") if result.answer_context else None
    if not intent:
        intent = (listing_focus(question) if domain == "LISTING" else
                  "AVAILABILITY" if domain == "APPOINTMENT" and not result.appointment_cards and bool((result.listing_context or {}).get("listingIds")) else "DETAIL")
    topics = list(dict.fromkeys([intent] + (result.answer_context.get("topics", []) if result.answer_context else [])))
    if domain == "LISTING":
        from homespace_ai.application.listing_table import requested_sections
        topics += requested_sections(question)
    elif domain == "CONTRACT":
        # Reuse the domain's existing topic rules; this adds no tool or model call.
        from homespace_ai.application.use_cases.contract_ask import TOPICS, GROUP_PATTERNS
        codes = {"financial": "FINANCIAL", "lifecycle": "TERM", "amendments": "AMENDMENTS"}
        topics += [codes[name] for name, pattern in TOPICS if name in codes and pattern.search(question)]
        if any(group == "CHARGES" and pattern.search(question) for group, pattern in GROUP_PATTERNS):
            topics.append("CHARGES")
        if "ký" in question.lower():
            topics.append("SIGN")
    elif domain == "APPOINTMENT":
        from homespace_ai.application.use_cases.appointment_ask import RESCHEDULE_WORDS, TOPIC_ACTIONS
        text = fold_text(question)
        if RESCHEDULE_WORDS.search(text):
            topics.append("RESCHEDULE")
        if TOPIC_ACTIONS.search(text):
            topics.append("GUIDE")
        if any(w in text for w in ("gio", "thoi gian", "luc nao")):
            topics.append("SCHEDULE")
        if any(w in text for w in ("phan hoi", "xac nhan", "trang thai")):
            topics.append("STATUS")
    meta = AnswerContext(domain, intent, topics, scope).metadata()
    result.answer_context = meta
    if result.status in ERRORS:
        result.suggested_questions = (["Tôi cần đăng nhập ở đâu để xem thông tin của mình?"] if result.status == "LOGIN_REQUIRED" else
                                      [] if result.status in {"OUT_OF_SCOPE", "NO_EVIDENCE"} else [question])
        return result
    recent = set()
    for m in history[-4:]:
        old = m.get("answerContext") or {}
        if old.get("domain") == domain and old.get("scope") == scope:
            recent.update(old.get("topics") or [])
    blocked = set(topics) | recent
    candidates = []
    def add(topic, text, available=True):
        if available and topic not in blocked:
            candidates.append((topic, text))
    if domain == "LISTING":
        state = context.get("searchState") or {}
        from homespace_ai.application import renter_guidance as guidance
        if result.status == "NEEDS_CLARIFICATION" or not scope:
            # Preserve precise clarification choices (e.g. inside vs outside amenities).
            if not result.suggested_questions:
                result.suggested_questions = guidance.suggested_questions(state, no_results=result.status == "NO_RESULTS")
            return result
        main = context.get("mainRecommendation")
        if intent == "PAGE_END":
            add("PAGE_BACK", "Quay lại", (state.get("pageHistory") or {}).get("index", 0) > 0)
            add("PAGE_FIRST", "Xem lại nhóm phòng đầu tiên")
        add("EXPLAIN_CHOICE", "Vì sao bạn ưu tiên phòng được chọn?", bool(main))
        add("NEARBY", "Các phòng trên có những tiện ích nào xung quanh?", intent != "COST")
        add("COST", "Trong các phòng này, các khoản phí hằng tháng khác nhau thế nào?", "charges" not in blocked)
        if intent == "COST":
            add("terms", "Tiền cọc và tiền cần chuẩn bị khi nhận các phòng này khác nhau thế nào?")
        if intent == "NEARBY":
            add("NEARBY_TARGET", "Bạn có thể so sánh theo đúng trường hoặc nơi làm việc mình cần đến không?")
        add("policies", "Các phòng này có điều kiện sinh hoạt hoặc nội quy nào cần lưu ý?")
        add("viewing", "Tôi muốn xem lịch có thể đi xem của phòng được gợi ý.", bool(main))
        # Search records matchedTotal; never offer pagination merely because a scope exists.
        more = context.get("hasMore")
        if more is None:
            previous = next((m.get("listingContext") for m in reversed(history) if m.get("role") == "assistant" and
                             (m.get("listingContext") or {}).get("listingIds") == scope), {}) or {}
            more = previous.get("hasMore")
            if more is not None:
                context["hasMore"] = more
        add("PAGE_NEXT", "Cho tôi xem thêm các phòng phù hợp với tiêu chí hiện tại được không?", more is True)
        pages = (state.get("pageHistory") or {}).get("pages") or []
        add("PAGE_BACK", "Quay lại", intent != "PAGE_END" and (state.get("pageHistory") or {}).get("index", 0) > 0 and len(pages) > 1)
    elif domain == "CONTRACT":
        cards = result.contract_cards
        text = fold_text(question)
        if len(cards) > 1:
            add("SELECT", "Tôi muốn xem chi tiết hợp đồng thứ nhất.")
        if len(cards) == 1:
            card = cards[0]
            signing = card.get("status") in {"DRAFT", "PENDING_REVIEW", "LANDLORD_SIGNATURE_PENDING", "TENANT_SIGNATURE_PENDING"}
            add("SIGN", "Hợp đồng này còn cần bên nào ký và tôi cần làm gì tiếp?", signing and "ky" not in text)
            add("TERM", "Ngày cuối cùng tôi còn thuê theo hợp đồng này là ngày nào?", "ngay" not in text and "het han" not in text)
            add("CHARGES", "Các khoản phí đang áp dụng trong hợp đồng này là gì?", not any(w in text for w in ("phi", "dien", "nuoc")))
            add("FINANCIAL", "Hợp đồng này còn khoản thanh toán nào chưa hoàn tất?", not signing and "thanh toan" not in text)
            add("AMENDMENTS", "Hợp đồng này có phụ lục nào đang áp dụng không?", "phu luc" not in text)
    elif domain == "APPOINTMENT":
        cards = result.appointment_cards
        text = fold_text(question)
        if intent in {"AVAILABILITY", "VIEWING_SCHEDULE"}:
            add("BOOK_GUIDE", "Tôi muốn đặt lịch xem phòng này, cần làm gì?")
        if intent == "VIEWING_SCHEDULE":
            add("AVAILABILITY", "Phòng này ngày mai xem được không?", bool(context.get("viewingListingId")))
        if len(cards) > 1:
            add("SELECT", "Cho tôi xem chi tiết lịch hẹn thứ nhất.")
        if len(cards) == 1:
            card = cards[0]
            add("SCHEDULE", "Lịch hẹn này hiện được xác nhận vào thời gian nào?", not any(w in text for w in ("gio", "thoi gian")))
            add("RESCHEDULE", "Giờ hiện tại và giờ xin đổi của lịch này khác nhau thế nào?", bool(card.get("rescheduleRequested")) and "doi" not in text)
            add("STATUS", "Lịch hẹn này đang chờ bên nào phản hồi?", card.get("status") == "PENDING" and "phan hoi" not in text and "xac nhan chua" not in text)
            add("GUIDE", "Với trạng thái hiện tại, tôi có thể thao tác gì với lịch này?", card.get("status") in {"PENDING", "CONFIRMED"} and "thao tac" not in text)
        add("LIST", "Tôi còn lịch xem nhà nào sắp tới?", "sap toi" not in text and intent not in {"AVAILABILITY", "VIEWING_SCHEDULE"})
    elif domain == "RENTAL_REVIEW":
        add("REVIEW_REASONS", "Vì sao hồ sơ này được ưu tiên hơn các hồ sơ còn lại?", "vi sao" not in fold_text(question))
        add("REVIEW_COST", "Các hồ sơ này khác nhau thế nào về thời hạn thuê và khoản thanh toán?")
    elif domain == "KNOWLEDGE":
        q = fold_text(question)
        if "coc" in q:
            add("SIGN_GUIDE", "Sau khi đặt cọc, tôi cần làm gì để ký hợp đồng thuê?")
            add("PAYMENT_GUIDE", "Tôi kiểm tra trạng thái khoản thanh toán của mình ở đâu?")
        elif "lich" in q:
            add("APPOINTMENT_GUIDE", "Tôi theo dõi chủ nhà phản hồi lịch xem ở đâu?")
        elif "hop dong" in q or "ky" in q:
            add("SIGN_GUIDE", "Tôi xem hợp đồng đang áp dụng và tiến trình ký ở đâu?")
    asked = {fold_text(question).strip(" .?!")}
    asked.update(fold_text(m.get("content", "")).strip(" .?!") for m in history[-4:] if m.get("role") == "user")
    result.suggested_questions = list(dict.fromkeys(text for _, text in candidates if fold_text(text).strip(" .?!") not in asked))[:4]
    return result
