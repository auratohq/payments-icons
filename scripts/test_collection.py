import hashlib
from io import BytesIO
import unittest

from PIL import Image, ImageDraw

from collection import check_entry, check_image, png_from_mini, redact_private_source_urls, spreadsheet_safe


def specimen(shape='rounded', hole=False):
    im = Image.new('RGBA', (48, 48), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    if shape == 'rounded':
        draw.rounded_rectangle((0, 0, 47, 47), radius=8, fill='white')
    elif shape == 'square':
        draw.rectangle((0, 0, 47, 47), fill='white')
    elif shape == 'circle':
        draw.ellipse((0, 0, 47, 47), fill='white')
    if hole:
        draw.rectangle((18, 18, 24, 24), fill=(0, 0, 0, 0))
    stream = BytesIO()
    im.save(stream, format='PNG')
    data = stream.getvalue()
    return data, {'id': 'pmt_test', 'name': 'Test', 'category': 'payment-methods',
                  'path': 'payment-methods/test.png', 'upstreamPath': '48/payment-methods/pmt_test.png.webp',
                  'aliases': [], 'width': 48, 'height': 48,
                  'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                  'source': {'kind': 'generated', 'url': None}}


def card_specimen():
    im = Image.new('RGBA', (48, 30), 'white')
    stream = BytesIO()
    im.save(stream, format='PNG')
    data = stream.getvalue()
    return data, {'id': 'crd_test', 'name': 'Test Card', 'category': 'cards',
                  'path': 'cards/test-card.png', 'upstreamPath': '48/cards/crd_test.png.webp',
                  'aliases': [], 'width': 48, 'height': 30,
                  'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                  'source': {'kind': 'generated', 'url': None}}


class ValidationTests(unittest.TestCase):
    def test_native_card_webp_keeps_decoded_pixels(self):
        image = Image.new('RGBA', (48, 30), (20, 80, 160, 255))
        source = BytesIO()
        image.save(source, format='WEBP', lossless=True)
        result = Image.open(BytesIO(png_from_mini(source.getvalue(), 'cards')))
        self.assertEqual(result.size, (48, 30))
        self.assertEqual(result.tobytes(), image.tobytes())

    def test_square_card_canvas_is_fitted_without_stretching(self):
        image = Image.new('RGBA', (48, 48))
        ImageDraw.Draw(image).rectangle((0, 8, 47, 39), fill='white')
        source = BytesIO()
        image.save(source, format='WEBP', lossless=True)
        result = Image.open(BytesIO(png_from_mini(source.getvalue(), 'cards')))
        self.assertEqual(result.size, (48, 30))
        self.assertEqual(result.getchannel('A').getbbox(), (1, 0, 46, 30))

    def test_mini_conversion_rejects_unexpected_source_format(self):
        data, _ = specimen()
        with self.assertRaises(ValueError):
            png_from_mini(data, 'payment-methods')

    def test_portrait_card_keeps_its_orientation_and_proportions(self):
        image = Image.new('RGBA', (48, 76), 'white')
        source = BytesIO()
        image.save(source, format='WEBP', lossless=True)
        result = Image.open(BytesIO(png_from_mini(source.getvalue(), 'cards')))
        self.assertEqual(result.size, (48, 30))
        self.assertEqual(result.getchannel('A').getbbox(), (14, 0, 33, 30))

    def test_square_icon_source_gets_the_pack_rounded_frame(self):
        image = Image.new('RGBA', (48, 48), 'white')
        source = BytesIO()
        image.save(source, format='WEBP', lossless=True)
        data = png_from_mini(source.getvalue(), 'banks')
        _, item = specimen()
        item.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        check_image(data, item)

    def test_nested_upstream_paths_are_checked(self):
        _, item = card_specimen()
        check_entry({**item, 'upstreamPath': '48/cards/review/card-a/image.png.webp'})
        for path in ('48/cards/../private.webp', '48/cards//image.webp', '48/banks/image.webp'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                check_entry({**item, 'upstreamPath': path})

    def test_rounded_square_is_accepted(self):
        data, item = specimen()
        check_entry(item)
        check_image(data, item)

    def test_square_and_circle_are_rejected(self):
        for shape in ('square', 'circle'):
            with self.subTest(shape=shape), self.assertRaises(ValueError):
                check_image(*specimen(shape))

    def test_card_can_keep_its_own_opaque_frame(self):
        check_image(*card_specimen())

    def test_transparent_interior_is_rejected(self):
        with self.assertRaises(ValueError):
            check_image(*specimen(hole=True))

    def test_tampered_bytes_are_rejected(self):
        data, item = specimen()
        with self.assertRaises(ValueError):
            check_image(data + b'changed', item)

    def test_path_traversal_and_hidden_layers_are_rejected(self):
        _, item = specimen()
        for path in ('../outside.png', 'payment-methods/../outside.png', '/payment-methods/test.png',
                     'payment-methods//test.png', 'v1/payment-methods/test.png', 'payment-methods/Test.png',
                     '48/payment-methods/pmt_test.png.webp'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                check_entry({**item, 'path': path})

    def test_source_url_cannot_execute_script(self):
        _, item = specimen()
        item['source']['url'] = 'javascript:alert(1)'
        with self.assertRaises(ValueError):
            check_entry(item)

    def test_csv_formula_is_escaped(self):
        self.assertEqual(spreadsheet_safe('=1+1'), "'=1+1")
        self.assertEqual(spreadsheet_safe('Visa'), 'Visa')

    def test_private_asset_url_becomes_repository_reference(self):
        _, item = specimen()
        item['source']['url'] = (
            'https://assets.example/storage/v1/object/public/entity-assets/'
            '48/payment-methods/pmt_test.png.webp'
        )
        self.assertEqual(redact_private_source_urls([item]), 1)
        self.assertIsNone(item['source']['url'])
        self.assertEqual(item['source']['reference'], 'payment-methods/test.png')


if __name__ == '__main__':
    unittest.main()
