"""Conservative room-change detection from administrator-authored scene text."""
import re


ROOMS = (
    r'кухн|кухон|kitchen', r'гостин|living room|lounge', r'спальн|bedroom',
    r'ванн|bathroom', r'кабинет|study room|office', r'балкон|balcony',
    r'прихож|коридор|hallway', r'улиц|двор|outdoors|outside',
)


def text(spec):
    return (str(spec.get('title', '')) + ' ' + str(spec.get('visual', ''))).lower()


def use_previous_location(previous, current):
    """A repeated room name is continuity; an explicit new room starts a new set."""
    before, after = text(previous), text(current)
    if re.search(r'нов\w* комнат|друг\w* комнат|смен\w* локац|new room|different room', after):
        return False
    current_rooms = {i for i, pattern in enumerate(ROOMS) if re.search(pattern, after)}
    previous_rooms = {i for i, pattern in enumerate(ROOMS) if re.search(pattern, before)}
    return not current_rooms or bool(previous_rooms and current_rooms == previous_rooms)
