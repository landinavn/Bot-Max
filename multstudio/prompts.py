"""Still-frame direction is separate from the approved animation script."""
from .repository import encoded
from .seed import SCENES


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


def scene_image_prompt(snap):
    spec = snap['spec']
    moment = spec.get('visual', '')
    camera = 'One fixed medium-wide camera view showing the cast together in one space.'
    for original, keyframe in zip(SCENES, KEYFRAMES):
        if spec.get('title') == original[0] and moment == original[3]:
            moment, camera = keyframe
            break
    # Never send the full spec (camera transitions, dialogue, sound, duration) to
    # the still-image model. Custom scenes select one instant from their visual.
    cast = [c['spec'] for c in snap['cast']]
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
    )
