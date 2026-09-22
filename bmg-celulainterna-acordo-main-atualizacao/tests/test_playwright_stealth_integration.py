import unittest
from unittest import mock

from core import browser


class TestPlaywrightStealthIntegration(unittest.TestCase):
    def test_aplicar_stealth_ao_contexto_usa_api_esperada(self):
        calls = []

        class FakeStealth:
            def apply_stealth_sync(self, obj):
                calls.append(obj)

        with mock.patch.object(browser, "Stealth", return_value=FakeStealth()):
            contexto = object()
            self.assertTrue(browser.aplicar_stealth_ao_contexto(contexto))
            self.assertEqual(calls, [contexto])

    def test_aplicar_stealth_ao_page_usa_api_esperada(self):
        calls = []

        class FakeStealth:
            def apply_stealth_sync(self, obj):
                calls.append(obj)

        with mock.patch.object(browser, "Stealth", return_value=FakeStealth()):
            pagina = object()
            self.assertTrue(browser.aplicar_stealth_ao_page(pagina))
            self.assertEqual(calls, [pagina])


if __name__ == "__main__":
    unittest.main()
