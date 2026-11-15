"""Local Christmas picture-frame artwork; the child's original photo is never modified."""
import math
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFilter

PHOTO_MAX_WIDTH=400
PHOTO_MAX_HEIGHT=720
FRAME_PAD=28


def create_child_frame(child, target):
    """Return fitted photo dimensions and save an RGBA border with a transparent opening."""
    with Image.open(child) as photo:
        ratio=min(PHOTO_MAX_WIDTH/photo.width,PHOTO_MAX_HEIGHT/photo.height)
        width=max(2,int(photo.width*ratio)//2*2)
        height=max(2,int(photo.height*ratio)//2*2)
    canvas_width,canvas_height=width+2*FRAME_PAD,height+2*FRAME_PAD
    scale=3
    frame=Image.new('RGBA',(canvas_width*scale,canvas_height*scale),(0,0,0,0))
    draw=ImageDraw.Draw(frame)
    palette=((108,65,17),(236,193,98),(255,239,176),(186,125,38),(244,208,119))
    def points(values):return [(round(x*scale),round(y*scale)) for x,y in values]
    for inset in range(FRAME_PAD):
        phase=inset/(FRAME_PAD-1)*(len(palette)-1)
        lo=min(int(phase),len(palette)-2);weight=phase-lo
        color=tuple(round(a+(b-a)*weight) for a,b in zip(palette[lo],palette[lo+1]))+(255,)
        box=tuple(v*scale for v in (inset,inset,canvas_width-1-inset,canvas_height-1-inset))
        draw.rounded_rectangle(box,radius=max(0,12-inset)*scale,outline=color,width=scale)
    # Short evergreen sprigs and berries stay on the border, outside the face/photo.
    for right,bottom in ((False,False),(True,False),(False,True),(True,True)):
        def at(x,y):return (canvas_width-x if right else x,canvas_height-y if bottom else y)
        draw.line(points([at(13,15),at(min(86,canvas_width/2-12),15)]),fill='#274c32',width=2*scale)
        for x in range(24,int(min(83,canvas_width/2-14)),10):
            for sign in (-1,1):
                draw.line(points([at(x,15),at(x-8,15+sign*7)]),fill='#386941',width=2*scale)
                draw.line(points([at(x+3,15),at(x-3,15+sign*5)]),fill='#60935a',width=scale)
        for x,y,r in ((15,15,4),(23,20,3),(14,24,3)):
            cx,cy=at(x,y)
            draw.ellipse(tuple(v*scale for v in (cx-r,cy-r,cx+r,cy+r)),fill='#b52e34')
            draw.ellipse(tuple(v*scale for v in (cx-r/2,cy-r/2,cx,cy)),fill='#f6b9a0')
    stars=Image.new('RGBA',frame.size,(0,0,0,0));sparkles=ImageDraw.Draw(stars)
    for cx,cy,r in ((canvas_width/2,14,7),(canvas_width/2,canvas_height-14,7),
                    (14,canvas_height/2,5),(canvas_width-14,canvas_height/2,5)):
        polygon=[]
        for i in range(8):
            angle=i*math.pi/4;radius=r if i%2==0 else r*.22
            polygon.append((cx+math.cos(angle)*radius,cy+math.sin(angle)*radius))
        sparkles.polygon(points(polygon),fill=(255,250,218,255))
    frame=Image.alpha_composite(frame,stars.filter(ImageFilter.GaussianBlur(3*scale)))
    frame=Image.alpha_composite(frame,stars)
    # Keep the central photo opening completely clear, including decoration blur.
    ImageDraw.Draw(frame).rectangle(tuple(v*scale for v in
        (FRAME_PAD,FRAME_PAD,FRAME_PAD+width-1/scale,FRAME_PAD+height-1/scale)),fill=(0,0,0,0))
    frame=frame.resize((canvas_width,canvas_height),Image.Resampling.LANCZOS)
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name('.frame-'+uuid4().hex+'.png')
    try:
        frame.save(temporary,format='PNG')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return width,height
