"""Approved mother greetings and occasion profiles for songs, cards and cartoons."""
TITLE = '🤱 День матери'
DETAILS_PROMPT = (
    '🤱 Как зовут маму и что вы хотите ей сказать?\n\n'
    'Напишите одним сообщением, по порядку:\n'
    '1. Имя мамы, которой адресована песня.\n'
    '2. За что хотите поблагодарить или что пожелать.\n'
    '3. Кто поздравляет (необязательно).\n\n'
    'Например: «Людмила; спасибо за заботу и поддержку, за наши тёплые семейные вечера; '
    'от дочери».\n\n'
    'До 500 символов.'
)
CARD_PROMPT = (
    "Mother's Day greeting card. Preserve the mother's identity, natural age and facial features "
    'from the reference. Cozy home at sunset, a vase of fresh flowers, warm tender atmosphere, '
    'elegant cream and soft rose palette. No birthday cakes, candles, numbers or New Year decor. '
    'Leave a large clean area at the bottom for the personal greeting. Do not add other people.'
)
CARTOON_DESCRIPTION = (
    '🤱 Ко Дню матери создадим маленький мультфильм на 60 секунд с героиней — вашей мамой! 💐\n\n'
    '🎵 За кадром прозвучит персональная песня с её именем.\n\n'
    '📸 Подготовьте одно фото мамы, где она одна и хорошо видно лицо.\n\nСтоимость — 590 ₽.'
)

# This is the approved song, not an example for free-form rewriting.
APPROVED_VERSE_1 = '''В окне родного дома свет не гаснет,
Куда б ни довелось нам повернуть.
С тобой любой обычный вечер — праздник,
Здесь можно от тревог передохнуть.
Для {name_genitive} эта песня — благодарность
За то, что нас умеешь понимать.
За веру в нас, за нежность и за радость,
За дом, где можно заново мечтать.'''
APPROVED_CHORUS = '''С Днём матери, мама родная!
Ты столько тепла отдала.
Пусть жизнь и тебя обнимает,
Как ты нас всегда берегла.
Спасибо за право собой оставаться,
За дом, где не нужно любви добиваться.'''
APPROVED_VERSE_2 = '''Мы выросли — у каждого заботы,
Свои дороги, планы и дела.
Но в каждом добром шаге, в каждом взлёте —
Твоя любовь, что силы нам дала.
Теперь и нам заботиться пора,
Чтоб ты жила спокойнее, счастливей.
Пусть для тебя вся жизнь будет добра,
А каждый день — теплее и красивей.'''
APPROVED_BRIDGE = '''Пусть годы птицей в небе пролетают,
А в сердце расцветают лишь цветы.
Пусть дни тебя заботой согревают,
И сбудется всё то, что хочешь ты.'''
PERSONALIZATION_INSTRUCTION = (
    'Ты персонализируешь уже утверждённую песню ко Дню матери. НЕ пиши новую песню. '
    'Верни только JSON с двумя ключами: name_genitive (имя мамы в родительном падеже '
    'после «Для», например Ольги, Людмилы) и personal_lines (массив из двух стихотворных '
    'строк или пустой массив). Первое имя в данных клиента — имя мамы. '
    'Если есть только обычная благодарность за заботу и поддержку, верни пустой массив: '
    'эти мысли уже есть в основе. Если есть конкретные детали, увлечение, пожелание или '
    'указание отправителя, вырази их в двух связных ритмичных строках, желательно с рифмой. '
    'Каждая строка до 140 символов; сохрани смысл, имена и правильные падежи. '
    'Для короткой версии эти две строки будут рядом со строкой благодарности по имени, '
    'для полной — в середине второго куплета. Не возвращай куплеты, припевы или теги. '
    'Не выдумывай факты, имена и состав семьи. Внуки/бабушка допустимы только если '
    'клиент просит их упомянуть; запрет клиента имеет приоритет. '
    'Остальные строки, включая все припевы, сохраняются программой без изменений.'
)


def render_approved_song(name_genitive, personal_lines, *, short=False, birthday=False):
    import re
    if (not isinstance(name_genitive,str) or not name_genitive.strip() or len(name_genitive)>80
            or not re.fullmatch(r"[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z '\-’]*",name_genitive)):
        raise ValueError('Не удалось подготовить имя мамы для песни.')
    if (not isinstance(personal_lines,list) or len(personal_lines) not in (0,2)
            or any(not isinstance(line,str) or not line.strip() or len(line)>140
                   or any(ch in line for ch in '\n\r[]{}') for line in personal_lines)):
        raise ValueError('Не удалось подготовить личные строки песни.')
    chorus=APPROVED_CHORUS.replace('С Днём матери,', 'С днём рождения,') if birthday else APPROVED_CHORUS
    verse1=APPROVED_VERSE_1.format(name_genitive=name_genitive.strip())
    if short:
        lines=verse1.splitlines()
        verse='\n'.join(lines[4:6]+personal_lines if personal_lines else lines[:2]+lines[4:6])
        return '[Verse 1]\n'+verse+'\n[Chorus]\n'+chorus
    verse2=APPROVED_VERSE_2.splitlines()
    if personal_lines:verse2[2:4]=personal_lines
    return ('[Verse 1]\n'+verse1+'\n[Chorus]\n'+chorus+
            '\n[Verse 2]\n'+'\n'.join(verse2)+'\n[Chorus]\n'+chorus+
            '\n[Bridge]\n'+APPROVED_BRIDGE+'\n[Chorus]\n'+chorus)


def minute_excerpt(text):
    """Only the first approved verse and chorus are shown for the one-minute cartoon."""
    import re
    verse=re.search(r'\[Verse 1\]\s*\n(.*?)(?=\n\[|$)',text,re.S)
    chorus=re.search(r'\[Chorus\]\s*\n(.*?)(?=\n\[|$)',text,re.S)
    if not verse or not chorus:
        raise ValueError('Не найден куплет и припев минутной песни.')
    return '[Verse 1]\n'+verse.group(1).strip()+'\n[Chorus]\n'+chorus.group(1).strip()


BIRTHDAY_TITLE = '🎂 День рождения мамы'
MOTHER_LABELS = frozenset((TITLE, BIRTHDAY_TITLE))
MOTHER_THEMES = frozenset(('theme_mother', 'theme_mother_bday'))
MOTHER_MODES = frozenset(('mode_mother', 'mode_mother_bday'))
MOTHER_SCENARIOS = frozenset(('audio_fun_mother', 'gift_scen_mother', 'audio_fun_mother_bday', 'gift_scen_mother_bday'))
BIRTHDAY_CARD_PROMPT = CARD_PROMPT.replace('Mother’s Day', "mother's birthday").replace("Mother's Day", "mother's birthday").replace(
    'No birthday cakes, candles, numbers or New Year decor.',
    'Elegant birthday flowers, a cake with candles and soft festive garlands. No age numbers or New Year decor.')
BIRTHDAY_CARTOON_DESCRIPTION = (
    '🎂 Ко дню рождения мамы создадим мультфильм на 60 секунд с героиней — вашей мамой! 💐\n\n'
    '🎵 За кадром прозвучит персональная поздравительная песня с её именем.\n\n'
    '🎉 Праздничная комната, торт со свечами и тёплые семейные воспоминания.\n\n'
    '📸 Подготовьте одно фото мамы, где она одна и хорошо видно лицо.\n\nСтоимость — 590 ₽.'
)


def mother_suffix(value):
    return 'mother_bday' if value in (BIRTHDAY_TITLE, 'theme_mother_bday', 'mode_mother_bday',
        'audio_fun_mother_bday', 'gift_scen_mother_bday', 'mother_bday') else 'mother'


def mother_title(value):
    return BIRTHDAY_TITLE if mother_suffix(value)=='mother_bday' else TITLE


def mother_description(value):
    return BIRTHDAY_CARTOON_DESCRIPTION if mother_suffix(value)=='mother_bday' else CARTOON_DESCRIPTION


def mother_event(data, event):
    value=data.get('cartoon_theme') or data.get('mode') or data.get('song_review_label')
    return event.replace('mother_', 'mother_bday_', 1) if mother_suffix(value)=='mother_bday' else event
