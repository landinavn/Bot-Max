"""Still-frame direction is separate from the approved animation script."""
from .repository import encoded
from .seed import SCENES


CLOSED_MOUTH_KEYFRAME = (
    '\nCRITICAL SOURCE-IMAGE REQUIREMENT: Every visible character must have their '
    'mouth fully closed, with lips gently touching and a relaxed jaw. '
    'No visible teeth, tongue or open mouth; no toothy grin, laughing mouth, '
    'shouting or mid-speech mouth shape. A subtle closed-lip smile is allowed. '
    'Express surprise, joy and excitement through eyes, eyebrows, gaze and body pose, '
    'never by opening the mouth. This requirement overrides any open-mouth expression '
    'in the visual description, world description or reference images. '
    'Preserve the exact character identity, face proportions, hairstyle and outfit; '
    'change only the mouth pose when a reference shows an open mouth. '
    'This is the resting pose of the SOURCE STILL IMAGE for later lip-sync, '
    'not an instruction to keep the mouth closed throughout the generated video. '
    'If the scene has no characters, keep it empty; do not add a person.'
)


# Match the original title AND visual: existing databases need no seed migration,
# and an administrator's changed scene must not silently receive stale direction.
KEYFRAMES = (
    ('Аня на кухне держит чашку кофе и удивлённо смотрит на календарь «НОЯБРЬ 2026». За окном первый снег.',
     'Один средний план Ани, камера на уровне глаз.'),
    ('Дима удобно сидит на диване с телефоном в руке, смотрит на Аню с довольной улыбкой. Аня рядом с диваном смотрит на мужа с обеспокоенным выражением. Оба находятся в одной гостиной.',
     'Один общий план гостиной с Аней и Димой вместе. Оба персонажа видны в едином пространстве, без отдельного портрета Димы.'),
    ('Аня с преувеличенным недоверием смотрит на Диму. Дима рядом улыбается с невозмутимым выражением.',
     'Один средний парный план: выразительное лицо Ани на переднем плане, Дима рядом в глубине того же пространства.'),
    ('Аня сидит за кухонным столом с ручкой над длинным списком подарков, свисающим со стола. Рядом блокнот, телефон, чашка кофе и разноцветные ручки.',
     'Один средний план под небольшим углом сверху: лицо Ани, её руки и список видны одновременно из одной точки съёмки.'),
    ('Маша стоит рядом с сидящей за кухонным столом Аней и с искренним любопытством смотрит на маму. Рука Ани с ручкой застыла над длинным списком подарков.',
     'Один средний парный план Ани и Маши за столом. Рука с ручкой находится в этом же кадре, без отдельной вставки.'),
    ('Аня у кухонного окна задумчиво смотрит на снег. Рядом на столе лежит список подарков.',
     'Один средний крупный план лица Ани в три четверти, окно со снегом видно на заднем плане того же кадра.'),
    ('Аня сидит у окна с ручкой над чистой страницей блокнота. Рядом чашка кофе. Выражение лица тёплое и задумчивое.',
     'Один спокойный средний план Ани у окна; блокнот и чашка находятся в том же пространстве.'),
)


def validate_scene_image_references(snap):
    """Also guard frozen/legacy jobs before a paid submission, not just quotes."""
    if snap['target'] != 'scene':
        return
    from .rules import strict
    if (not snap.get('cast') and not strict(snap['spec'])) or any(
        not c.get('assets', {}).get('image', {}).get('approved') for c in snap['cast']
    ):
        raise ValueError('Сначала создайте и утвердите эталоны всех персонажей сцены.')
    required=snap.get('spec',{}).get('prop_reference_scene_ids',[])
    if required and (len(snap.get('prop_references',[]))!=len(required) or any(not r['image'].get('approved') for r in snap.get('prop_references',[]))):
        raise ValueError('Сначала создайте и утвердите готовые изображения мечтаний, указанные для телефона или карты этой сцены.')
    if (snap['number'] > 1 or snap['spec'].get('location_reference_scene_id')) and snap.get('location_reference_required', True) and not (snap.get('location_reference') or {}).get('approved'):
        raise ValueError('Сначала создайте и утвердите изображение предыдущей сцены как эталон комнаты.')


def scene_image_prompt(snap, location_reference_url=None):
    spec = snap['spec']
    moment = spec.get('visual', '')
    camera = 'One fixed medium-wide camera view showing the cast together in one space.'
    from .rules import strict
    if strict(spec) and spec.get('speaker_id'):
        camera = ('One close-up or medium shot of ONLY the character who will speak in the later video. '
                  'Everyone else stays off screen. In this source still, the lips are gently closed.')
    if strict(spec) and spec.get('speaker_id') and spec.get('framing')=='medium':
        camera=('ONE waist-up MEDIUM SHOT of the adult speaking character alone. '
                'Show head, upper torso, both forearms, complete hands holding the phone, and part of the table. '
                'Keep visible space around the shoulders. Do not crop to a headshot, facial close-up, '
                'or extreme close-up. Keep the full hands and phone inside the central 80 percent of the image width, '
                'with clear side margins for vertical 9:16 export. Everyone else stays off screen. Lips gently closed.')
    if strict(spec) and not spec.get('speaker_id') and spec.get('framing')=='medium':
        camera=('ONE waist-up '+('MEDIUM TWO-SHOT' if len(snap.get('cast',[]))==2 else 'MEDIUM SHOT')+
                ', not separate close-ups. The specified characters, hands and phone fit completely inside '
                'the central 80 percent of the image width. Keep the table edge visible and clear side margins '
                'for vertical 9:16 export. One still moment, one camera.')
    if strict(spec) and spec.get('framing')=='wide':
        camera='ONE WIDE ESTABLISHING SHOT of the whole apartment, table, sofa and snowy window. No close-up.'
    if spec.get('camera_direction'):
        camera=('ONE continuous '+str(spec.get('framing','medium'))+' shot. '+spec['camera_direction']+
                ' Do not substitute a facial close-up. Keep the scripted environment and objects visible, '
                'with safe margins for vertical 9:16 export. '+
                ('Only the adult speaker is present. Their face and closed lips remain clearly visible, '
                 'unobscured and large enough for later lip-sync; a three-quarter angle is allowed, never a hidden face.'
                 if spec.get('speaker_id') else 'Show only the listed cast; no extra people.'))
    for original, keyframe in zip(SCENES, KEYFRAMES):
        if not spec.get('camera_direction') and spec.get('title') == original[0] and moment == original[3]:
            moment, camera = keyframe
            break
    # Never send the full spec (camera transitions, dialogue, sound, duration) to
    # the still-image model. Custom scenes select one instant from their visual.
    cast = [c['spec'] for c in snap['cast']]
    location_direction = (
        '\nCRITICAL: Keep the same interior background, lighting, and furniture as in '
        'the provided location reference. Do not change the room. '
        'Keep the exact same background, furniture, and room interior as in the reference image. '
        'The location reference is the LAST attached image, after all character references. '
        'It defines the apartment, not the cast: show only the characters listed for this scene. '
        'Preserve the arrangement of doors, windows, kitchen, sofa and table, furniture '
        'materials, colours and light direction. Stage the action in the scripted part '
        'of this SAME apartment; a sofa scene must stay by the same sofa, not move to '
        'the kitchen. Change only framing and character poses, never redesign the interior. '
        + ('\nLOCATION REFERENCE URL: ' + location_reference_url if location_reference_url else '')
        if snap.get('location_reference') else
        '\nNEW LOCATION: Create the room described in STILL MOMENT. Character reference '
        'backgrounds must not determine the room. Preserve character identities and outfits.'
    )
    return (
        'Create ONE vertical family 3D animated-film keyframe: a single uninterrupted '
        'full-frame image, one instant, one camera position and one coherent perspective. '
        'Never create a collage, storyboard, diptych, triptych, split screen, panels, '
        'grid, inset close-up, montage, dividing lines or borders. '
        f'Show exactly {len(cast)} characters from the supplied cast, each appearing '
        'exactly once; no duplicated faces, bodies or extra people. '
        'Use character reference images as exact identity/outfit models, NOT as '
        'photographic humans. Preserve faces, ages, hair, clothing and proportions. '
        'No subtitles, speech bubbles, title cards, logos or advertising. '
        'The final reference, if supplied, is the apartment: preserve layout, furniture, '
        'props and lighting. Reference images are identity/location guides, never panels '
        'to reproduce as a contact sheet. Choose only ONE frozen moment from the visual '
        'description; never illustrate successive actions as separate pictures. '
        '\nSTILL MOMENT:\n' + moment + '\nFIXED CAMERA:\n' + camera
        + '\nWORLD:\n' + encoded(snap['world'])
        + '\nCAST IN IMAGE ORDER:\n' + encoded(cast)
        + ('\nPROP IMAGE REFERENCES: Images between character references and the LAST location reference '
           'are approved dream artworks for the phone screen or printed pictures on the table. '
           'Use those actual artworks in the scripted props; do not invent app interfaces or replace '
           'the apartment with their scenery. These are not extra characters, panels or split-screen shots. '
           +encoded([dict(scene_id=r['scene_id'],title=r['title']) for r in snap.get('prop_references',[])])
           if snap.get('prop_references') else '')
        + location_direction
        + CLOSED_MOUTH_KEYFRAME
    )
