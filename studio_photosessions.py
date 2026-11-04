"""Studio birthday package: the original customer photo determines identity and age."""
import re

STUDIO_BIRTHDAY_TEMPLATE = 'birthday_girl'
STUDIO_BIRTHDAY_CALLBACK = 'photo_temp_birthday_girl'
STUDIO_BIRTHDAY_EXAMPLE = 'studio_birthday_girl.png'
BIRTHDAY_NUMBER_QUESTION = (
    'Какая цифра или число должны быть на торте? Отправьте только число от 0 до 99, '
    'например 7 или 13. Оно будет указано на свечах во втором кадре.'
)

IDENTITY = (
    'Create one photorealistic, ordinary age-appropriate studio birthday photograph of the SAME girl '
    'as in the original customer image. Preserve her recognizable face, actual age, skin tone, '
    'body proportions and natural build. Do not slim, enlarge, mature, or adultify her. '
    'The candle number is a requested prop, never a request to change her age or body. '
    'Keep the outfit, pose, camera framing, background and lighting specified for THIS frame. '
    'Hair follows the customer image: preserve its color, length, hairline and texture. If worn loose, '
    'give it soft natural waves and a neat studio finish; do not lengthen short hair or add extensions. '
    'If tied up or styled in an updo, preserve that kind of hairstyle and make it neater and prettier; '
    'do not let it down. Retain any natural fringe. Keep natural skin texture and a child-appropriate '
    'appearance, without adult glamour makeup. Correct anatomy: one person, two arms, two hands, '
    'five fingers per visible hand, no duplicated limbs. No logos, captions or watermarks. '
)

STUDIO_BIRTHDAY_GIRL_PROMPTS = (
    IDENTITY +
    'FRAME 1 — BLACK AND WHITE close-up studio portrait, vertical from head to upper waist. '
    'A plain light-grey seamless background; soft directional studio light and natural contrast. '
    'She wears an opaque white ribbed sleeveless crew-neck top, a white watch on the raised wrist. '
    'One arm curves naturally above the head, the hand resting lightly in her hair; the other arm '
    'stays outside this close crop. The head is gently tilted toward the viewer\'s right with a relaxed '
    'natural direct gaze at the camera and a soft closed-mouth expression. Hair is styled according '
    'to the customer image, with fine strands and volume if its length permits. No cake, candles or text. '
    'Real editorial portrait photography, subtle grain, no plastic skin.',
    IDENTITY +
    'FRAME 2 — COLOR birthday portrait on a very dark nearly black studio background. '
    'She wears a black leather jacket with long sleeves. She holds a small round cake in a white '
    'cardboard tray with baking paper, offering it toward the camera with BOTH hands supporting '
    'it naturally at its sides and underneath. The cake has white cream sides and a pale caramel '
    'or chocolate-decorated top. Place upright GOLD NUMBER-SHAPED burning candles spelling EXACTLY '
    '"{birthday_number}" from left to right: one candle per digit, no extra digits or candles. '
    'This number must be legible, physically formed by the candles, not printed over the photo. '
    'The camera focuses sharply on the cake, flames and numeral candles in the foreground. '
    'Her recognizable face stays softly out of focus behind the cake, head turned gently toward '
    'the viewer\'s left and slightly downward, with a warm natural smile. Candlelight softly warms '
    'the hands and face; shallow depth of field and a few subdued warm bokeh points above. '
    'Only the requested candle digits may appear as text. No Christmas decorations.',
    IDENTITY +
    'FRAME 3 — BLACK AND WHITE seated studio portrait, vertical three-quarter framing showing the '
    'head, torso and thighs, with part of a simple black stool visible. She wears an opaque black '
    'short sleeveless round-neck top, loose high-waisted washed-grey jeans and an oversized black '
    'blazer draped over both shoulders. One hand rests naturally on her thigh; the other lightly '
    'touches her hair near the shoulder. Her head turns toward the viewer\'s left and tips downward, '
    'eyes looking down to the side. A light airflow gives loose hair gentle movement only if it is '
    'loose in the customer photo; an updo remains an updo. Light-grey seamless background with a '
    'circular pool of studio light and a clear natural cast shadow behind her toward the viewer\'s '
    'right. Keep this a relaxed, neutral fashion portrait with realistic texture. No cake or text.',
)


def valid_birthday_number(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9]{1,2}', value) is not None


def birthday_prompts(number):
    if not valid_birthday_number(number):
        raise ValueError('Не указано корректное число для свечей.')
    return tuple(prompt.replace('{birthday_number}', number) for prompt in STUDIO_BIRTHDAY_GIRL_PROMPTS)


def is_studio_birthday_order(order):
    return bool(order and order.get('style') == 'photo_style_studio'
                and order.get('tariff') == 590 and order.get('template') == STUDIO_BIRTHDAY_TEMPLATE)


def is_studio_birthday_selection(data):
    return (data.get('photo_style') == 'photo_style_studio'
            and data.get('photo_tariff') == 'photo_tariff_590'
            and data.get('template') == STUDIO_BIRTHDAY_TEMPLATE)
