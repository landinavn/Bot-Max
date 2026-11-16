import pytest
import ded_moroz as dm


@pytest.mark.parametrize('name,achievement,quality', [('Машенька','научилась плавать','заботливая'),('Миша','научился плавать','заботливый')])
def test_greeting_has_no_fixed_child_gender_and_preserves_personal_facts(name,achievement,quality):
    text=dm.tagged_text(dict(name=name,fact1='любишь рисовать',fact2=achievement,fact3=quality,task='помогать маме'))
    assert 'Ты меня [drawls] очень радуешь!' in text
    assert 'порадовал' not in text
    assert name in text and achievement in text and quality in text
    assert '[raspy]' in text and '[drawls]' in text
    assert 'Ты меня очень радуешь!' in dm.EXPLANATION
