import unittest

from auto_promotion.backends.telegram import telegram_language_evidence


class TelegramDiscoveryLanguageTests(unittest.TestCase):
    def test_explicit_russian_group_rule_qualifies_without_reading_message_content(self):
        language, confidence = telegram_language_evidence(
            "Example AI", "Только русский язык [RU ONLY]", ""
        )
        self.assertEqual(language, "ru")
        self.assertGreaterEqual(confidence, 0.8)

    def test_russian_description_uses_multiple_distinctive_words(self):
        language, confidence = telegram_language_evidence(
            "Example AI Marketplace",
            "Доска объявлений которая упрощает поиск услуг в сфере AI. Реклама.",
            "",
        )
        self.assertEqual(language, "ru")
        self.assertGreaterEqual(confidence, 0.5)
        self.assertLess(confidence, 0.8)

    def test_natural_english_description_can_qualify(self):
        language, confidence = telegram_language_evidence(
            "AI art community",
            "Welcome to our group. Please share your work and your questions with the members.",
            "",
        )
        self.assertEqual(language, "en")
        self.assertGreaterEqual(confidence, 0.5)
        self.assertLess(confidence, 0.8)

    def test_explicit_english_rule_in_russian_is_recognized(self):
        self.assertEqual(
            telegram_language_evidence("AI", "Только английский язык", ""), ("en", 0.95)
        )

    def test_ambiguous_other_and_conflicting_language_evidence_stay_unknown(self):
        cases = [
            ("AI SDXL Flux", "Midjourney NSFW 18+", ""),
            ("Comunidad IA", "Hola amigos, compartir arte para todos", ""),
            ("Спільнота", "Тільки українська мова для учасників", ""),
            ("AI group", "English only / Только русский язык", ""),
            ("AI group", "English and Russian only", ""),
            ("AI group", "Not English only; multiple languages are welcome", ""),
            ("AI group", "Not RU only; multiple languages are welcome", ""),
            ("AI group", "Не только русский язык", ""),
        ]
        for values in cases:
            with self.subTest(values=values):
                self.assertEqual(telegram_language_evidence(*values), ("unknown", 0.0))

    def test_existing_chinese_sample_detection_still_works(self):
        language, confidence = telegram_language_evidence(
            "AI", "", "大家在群里讨论人工智能和视频创作交流"
        )
        self.assertEqual(language, "zh")
        self.assertGreaterEqual(confidence, 0.8)

    def test_strong_chinese_sample_takes_precedence_over_english_metadata_guess(self):
        language, _ = telegram_language_evidence(
            "AI art community",
            "Welcome to our group. Please share your work and your questions with the members.",
            "大家在群里讨论人工智能和视频创作交流",
        )
        self.assertEqual(language, "zh")
