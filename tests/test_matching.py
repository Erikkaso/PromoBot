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

if __name__ == '__main__':
    unittest.main()
