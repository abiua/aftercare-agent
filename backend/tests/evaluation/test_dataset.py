from io import BytesIO

from PIL import Image

from aftercare.evaluation.dataset import material_bytes


def test_fixture_image_is_deterministic_and_decodable():
    image_bytes = material_bytes("technical-test", "item")
    assert image_bytes == material_bytes("technical-test", "item")
    assert image_bytes != material_bytes("technical-test", "parcel")
    with Image.open(BytesIO(image_bytes)) as image:
        assert image.size == (8, 8)
        image.verify()
