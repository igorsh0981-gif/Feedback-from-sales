from app.masking.text import mask_text, merge_summaries, is_fio_label_line


def test_card_keeps_bin_and_last4():
    r = mask_text("Перевёл на карту 8600 1234 5678 9012, не дошло")
    assert "8600 12•• •••• 9012" in r.text
    assert "5678" not in r.text
    assert r.counts["card"] == 1


def test_card_without_spaces_and_dashes():
    assert "9860 12•• •••• 4444" in mask_text("9860123412344444").text
    assert "4278 32•• •••• 1111" in mask_text("карта 4278-3211-2222-1111").text


def test_phone_formats():
    r = mask_text("Мой номер +998 90 123 45 67, второй 998931234567, и 97-555-11-22")
    assert "+998 90 ••• •• 67" in r.text
    assert "+998 93 ••• •• 67" in r.text
    assert "97 ••• •• 22" in r.text
    assert "123 45" not in r.text
    assert r.counts["phone"] == 3


def test_phone_nine_digits_only_with_keyword():
    assert "90 ••• •• 67" in mask_text("телефон 901234567").text
    # 9 цифр без подписи — не телефон (может быть ИНН/номер операции без контекста)
    assert "901234567" in mask_text("операция 901234567").text


def test_account_pinfl_passport_inn():
    r = mask_text("Счёт 20208000123456789012, ПИНФЛ 31234567890123, паспорт AA1234567, ИНН 301234567")
    assert "[СЧЁТ]" in r.text and "[ПИНФЛ]" in r.text and "[ПАСПОРТ]" in r.text
    assert "ИНН [ИНН]" in r.text
    assert "301234567" not in r.text
    assert r.counts["account"] == r.counts["pinfl"] == r.counts["passport"] == r.counts["inn"] == 1


def test_sms_code_but_not_error_code():
    r = mask_text("Пришёл код 4821, но ввести не смог. Код ошибки 5003")
    assert "код [КОД]" in r.text
    assert "5003" in r.text
    r2 = mask_text("SMS kod: 123456")
    assert "123456" not in r2.text


def test_cvv_and_expiry_near_card():
    r = mask_text("Карта 8600 1111 2222 3333 срок 12/27 CVV 123")
    assert "[СРОК]" in r.text and "CVV [CVV]" in r.text
    # Без карты и без подсказки дата вида 10/26 не трогается
    assert "10/26" in mask_text("в 10/26 разделе").text
    # Полная дата не маскируется
    assert "21/10/2026" in mask_text("Карта 8600 1111 2222 3333, 21/10/2026").text


def test_fio_after_labels_ru_uz():
    r = mask_text("Получатель: Иванов Пётр Сергеевич, сумма 150 000 сум")
    assert "Получатель: [ФИО]" in r.text
    assert "150 000" in r.text  # суммы не маскируются
    r2 = mask_text("Qabul qiluvchi: Alisher Usmonov")
    assert "[ФИО]" in r2.text and "Alisher" not in r2.text
    r3 = mask_text("Karta egasi ALISHER USMONOV")
    assert "ALISHER" not in r3.text


def test_amounts_and_versions_untouched():
    text = "Не проходит перевод 12 000 000 сум на версии 4.2.0, ошибка 1003"
    r = mask_text(text)
    assert r.text == text
    assert not r.found


def test_no_double_masking_and_summary():
    r = mask_text("8600123412341234 и +998901112233")
    assert r.summary == "карта ×1, телефон ×1"
    assert merge_summaries("карта ×1", "карта ×2, телефон ×1") == "карта ×3, телефон ×1"


def test_fio_label_only_line():
    assert is_fio_label_line("Получатель:")
    assert is_fio_label_line("  Qabul qiluvchi ")
    assert not is_fio_label_line("Получатель: Иванов")
