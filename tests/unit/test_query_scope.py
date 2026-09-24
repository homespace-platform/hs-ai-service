from homespace_ai.application.use_cases.ask_use_cases import AskUseCases


def test_ownership_word_alone_does_not_make_policy_question_realtime():
    classifier = AskUseCases(
        session=None, settings=None, embedder=None, generative_client=None
    )
    assert not classifier.is_realtime_or_personal_query(
        "Quyền riêng tư của tôi được HomeSpace bảo vệ như thế nào?"
    )
    assert classifier.is_realtime_or_personal_query(
        "Tin đăng của tôi đã được duyệt chưa?"
    )
