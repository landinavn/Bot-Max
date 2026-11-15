from PIL import Image
import pytest

from ded_moroz_frame import create_child_frame, FRAME_PAD


@pytest.mark.parametrize('size,expected',[((500,500),(400,400)),((941,1672),(400,710))])
def test_frame_fits_without_cropping_and_keeps_photo_and_opening_clear(tmp_path,size,expected):
    child=tmp_path/'child.png';Image.new('RGB',size,'blue').save(child)
    original=child.read_bytes();target=tmp_path/'decorative-frame.png'
    assert create_child_frame(child,target)==expected
    assert child.read_bytes()==original
    with Image.open(target) as image:
        assert image.mode=='RGBA'
        assert image.size==(expected[0]+2*FRAME_PAD,expected[1]+2*FRAME_PAD)
        assert image.getpixel((image.width//2,image.height//2))[3]==0
        red,green,blue,alpha=image.getpixel((10,image.height//3))
        assert alpha>240 and red>100 and green>60 and red>blue
