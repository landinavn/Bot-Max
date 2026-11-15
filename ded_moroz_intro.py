"""A local festive title card and conservative Russian proper-name inflection."""
import math
import re
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

INTRO_SECONDS=3.0


@lru_cache(maxsize=1)
def morphology():
    import pymorphy3
    return pymorphy3.MorphAnalyzer()


def genitive_name(name):
    if not re.fullmatch(r'[А-Яа-яЁё]+(?:[ -][А-Яа-яЁё]+)*',name):return None
    result=[]
    for part in re.split(r'([ -])',name):
        if part in (' ','-'):
            result.append(part);continue
        forms=set()
        for parsed in morphology().parse(part):
            # Prefer the proper name, e.g. Любовь → Любови, not the noun любовь → любви.
            if parsed.is_known and 'Name' in parsed.tag and 'nomn' in parsed.tag:
                inflected=parsed.inflect({'gent'})
                if inflected:forms.add(inflected.word)
        if len(forms)!=1:return None
        value=forms.pop()
        result.append(value.upper() if part.isupper() else value.capitalize())
    return ''.join(result)


def recipient_line(name,name_genitive=None):
    name=name.strip()
    if not name:raise ValueError('Не указано имя для заставки.')
    if name_genitive is not None:
        if (not isinstance(name_genitive,str) or not name_genitive.strip()
            or len(name_genitive.strip())>120 or any(c in name_genitive for c in '\n\r\t[]{}')):
            raise ValueError('Введите имя в родительном падеже одной строкой, до 120 символов.')
        return 'для '+name_genitive.strip()
    declined=genitive_name(name)
    # Never guess endings for an unknown name or a pet name such as «Солнышко».
    return 'для '+declined if declined else name+', с Новым годом!'


def create_intro(name,target,*,name_genitive=None):
    recipient=recipient_line(name,name_genitive)
    font_path=Path(__file__).resolve().parent/'font.ttf'
    if not font_path.is_file():raise ValueError('Не найден font.ttf для новогодней заставки.')
    image=Image.new('RGB',(1920,1080));draw=ImageDraw.Draw(image)
    for y in range(1080):
        level=math.sin(math.pi*y/1080)
        draw.line((0,y,1919,y),fill=(round(7+8*level),round(29+26*level),round(30+16*level)))
    draw.rounded_rectangle((44,44,1875,1035),radius=42,outline='#cda84f',width=3)
    draw.rounded_rectangle((58,58,1861,1021),radius=32,outline='#776437',width=1)
    # Evergreen corners, gold stars and ornaments; no snow and no overlay on HeyGen footage.
    for right,bottom in ((False,False),(True,False),(False,True),(True,True)):
        def at(x,y):return (1920-x if right else x,1080-y if bottom else y)
        draw.line((at(65,80),at(360,145)),fill='#487c54',width=8)
        for x in range(95,345,23):
            y=80+(x-65)*65/295
            draw.line((at(x,y),at(x-28,y-35)),fill='#548a5d',width=5)
            draw.line((at(x,y),at(x-36,y+20)),fill='#2f6545',width=6)
        cx,cy=at(180,170)
        draw.line((at(180,75),at(180,130)),fill='#e4c67a',width=3)
        draw.ellipse((cx-37,cy-37,cx+37,cy+37),fill='#9f323b',outline='#eccf87',width=3)
        draw.arc((cx-24,cy-24,cx+24,cy+24),190,270,fill='#efbd9e',width=5)
    for cx,cy,r in ((960,140,16),(960,965,12),(160,530,11),(1760,530,11),(350,260,7),(1570,260,7)):
        points=[]
        for i in range(8):
            angle=i*math.pi/4;radius=r if i%2==0 else r*.22
            points.append((cx+math.cos(angle)*radius,cy+math.sin(angle)*radius))
        draw.polygon(points,fill='#fff0b8')
    def centered(text,box,size,color):
        # Fit wrapped text to a fixed safe area; render strings as data, never as FFmpeg expressions.
        x,y,w,h=box
        for current in range(size,23,-2):
            font=ImageFont.truetype(str(font_path),current)
            lines=[]
            for paragraph in text.splitlines():
                line=''
                for word in paragraph.split():
                    candidate=(line+' '+word).strip()
                    if line and draw.textlength(candidate,font=font)>w:
                        lines.append(line);line=''
                    # Exceptionally long tokens are split only when a whole word cannot fit.
                    for char in ((' ' if line else '')+word):
                        if line and draw.textlength(line+char,font=font)>w:
                            lines.append(line.strip());line=''
                        line+=char
                if line:lines.append(line.strip())
            rendered='\n'.join(lines)
            bounds=draw.multiline_textbbox((0,0),rendered,font=font,spacing=12,align='center',stroke_width=1)
            if bounds[3]-bounds[1]<=h:break
        draw.multiline_text((x+(w-(bounds[2]-bounds[0]))/2-bounds[0],y+(h-(bounds[3]-bounds[1]))/2-bounds[1]),
            rendered,font=font,fill=color,spacing=12,align='center',stroke_width=1)
    centered('НОВОГОДНЕЕ\nПОЗДРАВЛЕНИЕ',(230,290,1460,235),100,'#eed28c')
    centered(recipient,(230,555,1460,215),138,'#fff3d1')
    centered('от Деда Мороза',(300,810,1320,85),66,'#dfbe72')
    target.parent.mkdir(parents=True,exist_ok=True)
    image.save(target,format='PNG')
    target.with_suffix('.txt').write_text('Новогоднее поздравление\n'+recipient+'\nот Деда Мороза',encoding='utf-8')
    return recipient
