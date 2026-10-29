import unittest

from dedup import (
    cluster_offers, codes, extract_prices, offer_price, product_ids, same_offer, similarity, specs,
)

ANUNCIO_A = (
    '🔥 PLACA DE VIDEO RTX 5060 8GB GDDR7 ASUS DUAL\n'
    'De R$ 2.799,00 por R$ 2.199,90 no pix\n'
    'https://amzn.to/abc123 -> https://www.amazon.com.br/dp/B0CXYZ1234'
)
ANUNCIO_B = (
    'Baixou! Placa de video Asus Dual RTX 5060 8GB GDDR7\n'
    'Apenas R$ 2.149,00 a vista ou 10x de R$ 229,90\n'
    'Compre aqui: https://www.amazon.com.br/dp/B0CXYZ1234?tag=outro-afiliado'
)
ANUNCIO_C = (
    'Notebook Lenovo IdeaPad 3 Ryzen 5 16GB 512GB SSD\n'
    'por R$ 2.299,00 no boleto'
)


class PrecoTests(unittest.TestCase):
    def test_milhar_e_centavos(self):
        self.assertEqual(extract_prices('por R$ 1.999,90'), [1999.90])

    def test_ponto_como_milhar(self):
        self.assertEqual(extract_prices('R$ 2.200'), [2200.0])

    def test_ponto_como_decimal(self):
        self.assertEqual(extract_prices('R$ 19.90'), [19.90])

    def test_virgula_com_um_digito(self):
        self.assertEqual(extract_prices('R$ 10,5'), [10.5])

    def test_parcelas_ignoradas(self):
        self.assertEqual(extract_prices('R$ 1.200 ou 12x de R$ 100'), [1200.0])

    def test_preco_final_vence_o_riscado(self):
        self.assertEqual(offer_price('De R$ 2.799,00 por R$ 2.199,90'), 2199.90)

    def test_sem_marcacao_usa_o_menor(self):
        self.assertEqual(offer_price('R$ 2.799,00 | R$ 2.199,90'), 2199.90)

    def test_sem_preco(self):
        self.assertIsNone(offer_price('Oferta relampago sem valor divulgado'))


class SemelhancaTests(unittest.TestCase):
    def test_mesmo_produto_textos_diferentes(self):
        self.assertTrue(same_offer(ANUNCIO_A, ANUNCIO_B))

    def test_produtos_diferentes(self):
        self.assertFalse(same_offer(ANUNCIO_A, ANUNCIO_C))

    def test_codigo_de_produto_decide(self):
        amazon = 'Fone XYZ https://www.amazon.com.br/dp/B0AAAA1111'
        outro = 'Fone XYZ https://www.amazon.com.br/dp/B0BBBB2222?tag=afiliado'
        self.assertEqual(product_ids(amazon), {'b0aaaa1111'})
        # Textos quase identicos, mas o codigo do produto diz que sao itens distintos.
        self.assertGreater(similarity(amazon, outro), 0.8)
        self.assertFalse(same_offer(amazon, outro))

    def test_mercado_livre_ignora_hifen(self):
        self.assertEqual(product_ids('https://produto.mercadolivre.com.br/MLB-123456789-tv'),
                         product_ids('https://mercadolivre.com/sec/x?item=MLB123456789'))

    def test_preco_e_link_nao_afetam_semelhanca(self):
        base = 'Monitor LG UltraGear 27 polegadas 180Hz'
        self.assertGreater(
            similarity(base + ' por R$ 1.199 https://lg.com/a?tag=um',
                       base + ' por R$ 1.399 https://lg.com/b?tag=dois'),
            0.9,
        )


class VariacaoTests(unittest.TestCase):
    """Variacoes do mesmo modelo nao podem ser tratadas como anuncio repetido."""

    def test_capacidades_diferentes(self):
        self.assertEqual(specs('SSD Kingston NV3 1TB NVMe'), {'tb': {'1'}})
        self.assertFalse(same_offer('SSD Kingston NV3 1TB NVMe PCIe 4.0 R$ 349',
                                    'SSD Kingston NV3 2TB NVMe PCIe 4.0 R$ 649'))

    def test_tamanhos_diferentes(self):
        self.assertFalse(same_offer('Smart TV Samsung 50 polegadas 4K Crystal',
                                    'Smart TV Samsung 65 polegadas 4K Crystal'))

    def test_litros_e_l_sao_a_mesma_unidade(self):
        self.assertEqual(specs('Air Fryer 4L'), specs('Air Fryer 4 litros'))

    def test_modelos_diferentes_da_mesma_marca(self):
        self.assertIn('g502', codes('Mouse Logitech G502 Hero por R$ 199'))
        self.assertFalse(same_offer('Mouse Logitech G502 Hero por R$ 199',
                                    'Mouse Logitech G203 Lightsync por R$ 129'))
        self.assertFalse(same_offer('Fone JBL Tune 510BT por R$ 199',
                                    'Fone JBL Tune 520BT por R$ 249'))

    def test_modelo_extra_nao_separa_anuncio_igual(self):
        # Um canal detalha mais que o outro, mas os modelos citados se cruzam.
        self.assertTrue(same_offer(
            'Notebook Dell Inspiron 15 i5 8GB 256GB R$ 2.799',
            'Notebook Dell Inspiron 15 3520 Core i5 8GB RAM 256GB SSD Windows 11 R$ 2.699'))


class AgrupamentoTests(unittest.TestCase):
    def test_agrupa_e_separa(self):
        grupos = cluster_offers([ANUNCIO_A, ANUNCIO_C, ANUNCIO_B])
        self.assertEqual(grupos, [[0, 2], [1]])

    def test_anuncio_unico(self):
        self.assertEqual(cluster_offers([ANUNCIO_C]), [[0]])

    def test_lote_vazio(self):
        self.assertEqual(cluster_offers([]), [])

    def test_menor_preco_do_grupo(self):
        textos = [ANUNCIO_A, ANUNCIO_B]
        (grupo,) = cluster_offers(textos)
        vencedor = min(grupo, key=lambda i: offer_price(textos[i]))
        self.assertEqual(vencedor, 1)


if __name__ == '__main__':
    unittest.main()
