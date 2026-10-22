from app.pipeline.prefilter import detect_noise

ON = {"greetings": True, "reactions": True, "organization": True}


def cat(text, kind="text", media=False, prefs=ON):
    r = detect_noise(text, kind, media, prefs)
    return r.category if r else None


def test_greetings_and_reactions_filtered():
    assert cat("Всем доброе утро!") == "greetings"
    assert cat("Assalomu alaykum") == "greetings"
    assert cat("Спасибо 👍") == "reactions"
    assert cat("👍👍") == "reactions"
    assert cat("+") == "reactions"
    assert cat("rahmat") == "reactions"
    assert cat("", kind="sticker") == "reactions"


def test_organization_filtered():
    assert cat("Коллеги, созвон сегодня в 15:00?") == "organization"


def test_feedback_never_filtered():
    assert cat("У меня снова не работает") is None
    assert cat("Спасибо, но перевод так и не прошёл") is None
    assert cat("Доброе утро! Не открывается вклад") is None
    assert cat("Karta ochilmayapti") is None
    assert cat("Хотелось бы видеть комиссию сразу") is None
    assert cat("ок", media=True) is None  # есть скрин


def test_disabled_category_goes_to_analysis():
    assert cat("Всем доброе утро!", prefs={"greetings": False, "reactions": True, "organization": True}) is None


def test_ambiguous_goes_to_analysis():
    assert cat("А у кого-нибудь ещё так?") is None


def test_empty_service_message_filtered():
    assert detect_noise("", "other", False, ON)
    assert detect_noise("", "text", False, ON)
    assert detect_noise("", "document", False, ON) is None  # файл без подписи — в анализ
    assert detect_noise("", "photo", True, ON) is None
