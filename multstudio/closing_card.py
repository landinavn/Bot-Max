"""Local Russian end card with a real scannable QR. No image generation calls."""
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont,ImageOps,ImageFilter,ImageEnhance
from config import validate_bot_link

SECONDS=5


def create(destination,bot_url,background=None):
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M
    bot_url=validate_bot_link(bot_url)
    if background:
        with Image.open(background) as photo:
            canvas=ImageOps.fit(photo.convert('RGB'),(1080,1920)).filter(ImageFilter.GaussianBlur(6))
        canvas=ImageEnhance.Brightness(canvas).enhance(.7).convert('RGBA')
    else:
        canvas=Image.new('RGBA',(1080,1920),'#554b3c')
    panel=Image.new('RGBA',canvas.size);d=ImageDraw.Draw(panel)
    d.rounded_rectangle((76,200,1004,1760),radius=45,fill=(255,248,228,242),outline='#e3bb66',width=5)
    canvas=Image.alpha_composite(canvas,panel);d=ImageDraw.Draw(canvas)
    font_path=Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    def line(text,y,size,color='#4a3527'):
        font=ImageFont.truetype(str(font_path),size)
        while d.textlength(text,font=font)>800:
            size-=1;font=ImageFont.truetype(str(font_path),size)
        d.text((540,y),text,font=font,fill=color,anchor='mt')
    line('Маленькие',320,78);line('чудеса Ани',420,78)
    line('Продолжение следует…',620,53)
    line('А о чём мечтаете вы?',730,45)
    line('Создайте свою карту желаний',815,38)
    qr=qrcode.QRCode(error_correction=ERROR_CORRECT_M,box_size=12,border=4)
    qr.add_data(bot_url);qr.make(fit=True)
    qr.box_size=max(2,min(12,480//(qr.modules_count+8)))
    code=qr.make_image(fill_color='black',back_color='white').convert('RGBA')
    # Integral pixels per QR module and an intact white quiet zone; no decorations over the code.
    canvas.alpha_composite(code,((1080-code.width)//2,970))
    line('Откройте бота',1510,49)
    line('Наведите камеру на QR-код',1595,35)
    canvas.convert('RGB').save(destination,format='PNG')
    return destination
