import unittest
from matching import matches, matching_keywords


class MatchingTests(unittest.TestCase):
    def test_case_accents(self):
        self.assertTrue(matches('placa de video', 'PLACÁ DE VÍDEO RTX 5060'))

    def test_punctuation_as_separator(self):
        self.assertTrue(matches('RTX 5060', 'Oferta RTX-5060 por R$ 1.999'))

    def test_no_substring_in_other_words(self):
        self.assertFalse(matches('ram', 'Programa de descontos'))

    def test_no_partial_model(self):
        self.assertFalse(matches('RTX 5060', 'RTX 5060Ti por R$ 2.200'))

    def test_multiple_keywords(self):
        self.assertEqual(matching_keywords(['ssd', 'notebook'], 'Oferta de SSD NVMe'), ['ssd'])

class LimiteDePrecoTests(unittest.TestCase):
    LIMITS = {'rtx 5060': 2000.0, 'ssd': None}

    def _found(self, text, price):
        return matching_keywords(list(self.LIMITS), text, self.LIMITS, price)

    def test_abaixo_do_limite_passa(self):
        self.assertEqual(self._found('RTX 5060 por R$ 1.999', 1999.0), ['rtx 5060'])

    def test_no_limite_passa(self):
        self.assertEqual(self._found('RTX 5060 por R$ 2.000', 2000.0), ['rtx 5060'])

    def test_acima_do_limite_bloqueia(self):
        self.assertEqual(self._found('RTX 5060 por R$ 2.001', 2000.01), [])

    def test_sem_limite_nao_filtra(self):
        self.assertEqual(self._found('SSD NVMe R$ 9.999', 9999.0), ['ssd'])

    def test_anuncio_sem_preco_passa(self):
        self.assertEqual(self._found('RTX 5060 em oferta', None), ['rtx 5060'])


if __name__ == '__main__':
    unittest.main()
