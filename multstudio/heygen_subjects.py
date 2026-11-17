"""Prevent new HeyGen submissions for explicitly underage cast; keep archived media."""
import re


def validate(snap):
    for character in snap.get('cast',[]):
        spec=character['spec']
        age=spec.get('age')
        if age is None:
            match=re.search(r'(?<!\d)(\d{1,3})\s*(?:лет|года?|years?\s+old)\b',spec.get('description',''),re.I)
            age=match.group(1) if match else None
        try:
            age=float(age) if age is not None and not isinstance(age,bool) else None
        except (TypeError,ValueError):
            age=None
        if age is not None and 0<=age<18:
            raise ValueError('HeyGen не допускает говорящие аватары младше 18 лет, включая мультперсонажей. '
                'Передайте реплику взрослому персонажу или используйте готовый клип из разрешённого инструмента. '
                'Кадр с Машей можно оставить немым без отправки в HeyGen.')
